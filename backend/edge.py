"""Local fridge behaviour. Remote state is learned only from signed events."""
import os
import random

from .csv_data import data_cabinet
from .thermal import PhysicalCabinet, ThermalKalmanObserver

# Local reflex: if the door stays open this many simulated minutes (one tick each), the
# fridge closes it itself with its auto-closer. This works with no network and no AI.
DOOR_AUTO_CLOSE_MINUTES = int(os.getenv("DOOR_AUTO_CLOSE_MINUTES", "15"))

# A target override (the attack) raises the target by this much, e.g. 3°C becomes 12°C.
TARGET_OVERRIDE_OFFSET = 9
# Local reflex: if the target has been overridden and the real temperature passes the
# safe setting plus this margin (8°C for a 3°C fridge, the UK chilled limit), the fridge
# restores its own safe target. Again: no network and no AI needed.
SAFE_MARGIN_C = 5

# Safe fixes that may be requested remotely (by the AI or staff). The fridge still
# carries each one out itself and logs it as a signed event.
REMEDIATIONS = {"close_door", "protective_mode", "restore_target"}

# Attacks arrive over the network. A fridge that is cut off can't receive them.
ATTACKS = {"spoof_sensor", "override_target", "bad_setpoint"}


class EdgeController:
    def __init__(self, node, target=3.0, capacity=60, stock=35, frozen=False, plant=None):
        self.node = node
        # Real recorded data when available (see csv_data.py), otherwise the simulator.
        # Capacity and stock are only used inside the simulator's physics (thermal mass).
        cabinet = plant or data_cabinet(node.fid)
        if cabinet:
            target = cabinet.target
        self.plant = cabinet or PhysicalCabinet(node.fid, target, frozen, capacity, stock, random.Random(node.fid))
        self.observer = ThermalKalmanObserver(target, frozen)
        # Independent prediction must never fuse the sensor under investigation.
        # Otherwise a plausible-but-frozen reading drags the detector toward itself.
        self.safety_observer = ThermalKalmanObserver(target, frozen)
        self.safe_target = target
        self.sensor_window = []
        self.t = 0
        self.compressor = False
        self.mismatch = 0
        self.recovery = 0
        self.security = "normal"
        self.readings_ok = True
        self.history = []
        self.acked = set()
        self.distrusted_sensors = set()  # neighbours whose temperature readings we've stopped trusting
        self.paused = False
        self.running = True
        self.last = {}
        self.outbox = []  # alerts to publish on the next tick
        self.door_open_ticks = 0
        self.was_door_open = False
        self.forced_protective = False
        self.target_was_overridden = False
        self.restored_by_action = False

    @property
    def target_overridden(self):
        return abs(self.plant.target - self.safe_target) > 0.01

    def command(self, command):
        if command == "spoof_sensor":
            self.plant.spoofed = not self.plant.spoofed
        elif command == "open_door":
            self.plant.door_open = not self.plant.door_open
        elif command == "override_target":
            # The attack: someone gets past the command checks and changes the target.
            # Pressing it again undoes the override (as a member of staff would).
            self.plant.target = (self.safe_target if self.target_overridden
                                 else self.safe_target + TARGET_OVERRIDE_OFFSET)
        elif command == "bad_setpoint":
            return {"rejected": True, "reason": "Unsafe remote setpoint rejected; local target retained"}
        elif command == "pause":
            self.paused = not self.paused
        elif command == "close_door":
            if not self.plant.door_open:
                return {"ok": True, "action": command, "message": "The door was already closed"}
            self.plant.door_open = False
            self.door_open_ticks = 0
            return self._act(command, "Door closed remotely with the auto-closer")
        elif command == "protective_mode":
            self.forced_protective = True
            return self._act(command, "Protective mode: cooling from the independent estimate instead of the sensor")
        elif command == "restore_target":
            if not self.target_overridden:
                return {"ok": True, "action": command, "message": "The target was already at its safe setting"}
            self.plant.target = self.safe_target
            self.restored_by_action = True
            return self._act(command, f"Target temperature restored to its safe setting of {self.safe_target:g}°C")
        else:
            raise ValueError("Unknown fridge command")
        return {"ok": True}

    def starts_attack(self, command):
        """True if this command would start an attack (undoing one is always allowed)."""
        if command == "spoof_sensor":
            return not self.plant.spoofed
        if command == "override_target":
            return not self.target_overridden
        return command in ATTACKS

    def enter_safe_mode(self):
        """Cut off from the network: fall back to the fridge's own safe default settings."""
        if self.target_overridden:
            self.plant.target = self.safe_target
            self.restored_by_action = True
            self.outbox.append({"action": "safe_mode_restore",
                                "reason": f"Cut off from the network, so it switched to safe mode and restored "
                                          f"its safe target of {self.safe_target:g}°C"})

    def _act(self, command, message):
        self.outbox.append({"action": command, "reason": message})
        return {"ok": True, "action": command, "message": message}

    async def tick(self):
        while self.outbox:
            await self.node.emit("alert", self.outbox.pop(0))
        if self.paused:
            return
        self.t += 1
        p = self.plant
        p.compressor_on = self.compressor
        p.controller_uses_sensor = self.security != "protective"
        reported, power, door = p.step_physics()
        self.door_open_ticks = self.door_open_ticks + 1 if door else 0
        if door and not self.was_door_open:
            self.outbox.append({"reason": "Door opened"})
        self.was_door_open = door
        # Watchdog: the fridge knows its own safe setting and notices if the target changes.
        overridden = self.target_overridden
        if overridden and not self.target_was_overridden:
            self.outbox.append({"fault": "target", "target": p.target,
                                "reason": f"Target temperature changed to {p.target:g}°C; its safe setting is {self.safe_target:g}°C"})
        elif not overridden and self.target_was_overridden and not self.restored_by_action:
            self.outbox.append({"fault": "target", "resolved": True,
                                "reason": f"Target temperature back to its safe setting of {self.safe_target:g}°C"})
        if not overridden:
            self.restored_by_action = False
        self.target_was_overridden = overridden
        # Independent estimate: from another sensor in the same case with real data,
        # or from compressor power and a thermal model in the simulator.
        real_data = hasattr(p, "independent_estimate")
        if real_data:
            model_estimate = p.independent_estimate()
        else:
            model_estimate, _ = self.safety_observer.update(reported, power, door, 19.0, False)
        # Thresholds: calibrated per fridge from its own data, or the simulator's defaults.
        mismatch_c, agree_c = getattr(p, "mismatch_c", 2.5), getattr(p, "agree_c", 1.0)
        gap = abs(reported - model_estimate)
        self.sensor_window = (self.sensor_window + [reported])[-5:]
        # A frozen spoof can briefly match the model during a cooling cycle.
        # Require both agreement and a changing sensor before trusting it again.
        sensor_changes = (len(self.sensor_window) == 5
                          and max(self.sensor_window) - min(self.sensor_window) > getattr(p, "change_c", .4))
        self.mismatch = self.mismatch + 1 if gap > mismatch_c else 0
        self.recovery = self.recovery + 1 if gap < agree_c and sensor_changes else 0
        previous = self.security
        if self.mismatch >= 7:
            self.security = "protective"
        elif self.mismatch >= 2 and self.security != "protective":
            self.security = "suspicious"
        elif self.recovery >= 5:
            self.security = "normal"
        if self.forced_protective:
            # Requested remotely: stay protective until the sensor proves trustworthy again.
            if self.recovery >= 5 and self.mismatch == 0:
                self.forced_protective = False
            else:
                self.security = "protective"
        if real_data:
            fused = reported
        else:
            fused, _ = self.observer.update(reported, power, door, 19.0,
                                           self.security == "normal" and gap <= mismatch_c)
        estimate = model_estimate if self.security != "normal" else fused
        control = estimate if self.security == "protective" else reported
        if control > p.target + 0.8:
            self.compressor = True
        elif control < p.target - 0.8:
            self.compressor = False
        defrost = getattr(p, "defrost", False)
        self.readings_ok = estimate <= self.safe_target + 3 or defrost
        if previous != self.security:
            await self.node.emit("alert", {
                "reason": "Sensor agrees with model again" if self.security == "normal" else "Sensor/model mismatch",
                "security": self.security})
        await self.reply_to_neighbours(estimate)
        if door and self.door_open_ticks >= DOOR_AUTO_CLOSE_MINUTES:
            p.door_open = False
            self.door_open_ticks = 0
            self.was_door_open = False
            self.outbox.append({"action": "auto_close_door",
                                "reason": f"Door open {DOOR_AUTO_CLOSE_MINUTES} minutes, so the fridge closed it itself"})
        if overridden and estimate >= self.safe_target + SAFE_MARGIN_C - 0.5:  # act before the limit
            p.target = self.safe_target
            self.restored_by_action = True
            self.outbox.append({"action": "auto_restore_target",
                                "reason": f"Food was about to pass {self.safe_target + SAFE_MARGIN_C:g}°C, so the fridge "
                                          f"restored its own safe target of {self.safe_target:g}°C"})
        self.history.append({"t": self.t, "reported": reported, "estimate": estimate, "actual": p.true_temp})
        self.history = self.history[-60:]
        self.last = {"id": self.node.fid, "t": self.t, "target": p.target, "safe_target": self.safe_target,
                     "target_overridden": self.target_overridden, "frozen": p.frozen,
                     "reported": reported, "estimate": estimate,
                     "actual": p.true_temp, "power_watts": power, "compressor": self.compressor,
                     "door": door, "door_open_minutes": self.door_open_ticks,
                     "distrusted_sensors": sorted(self.distrusted_sensors), "spoofed": p.spoofed,
                     "security": self.security, "paused": self.paused,
                     "sensor_gap": gap, "model_estimate": model_estimate,
                     "control_source": "model" if self.security == "protective" else "sensor",
                     "defrost": defrost, "data_file": getattr(p, "file", None),
                     "chart_range": getattr(p, "chart_range", None),
                     "estimate_source": "return air sensor" if real_data else "thermal model"}
        await self.node.emit("telemetry", self.last)

    async def reply_to_neighbours(self, estimate):
        """When a directly connected neighbour raises a warning, check ourselves and reply.

        The reply says what we decided (for a sensor fault: stop trusting that fridge's
        temperature readings until it reports the sensor is working again) and gives one
        piece of advice from simple local rules, so it works with no AI and no server.
        It's a signed event that travels back over the same peer link.
        """
        peers = getattr(self.node, "peers", {})
        for event in list(self.node.events.values()):
            origin, data = event["origin"], event["data"]
            if event["kind"] != "alert" or origin == self.node.fid or origin not in peers:
                continue
            if data.get("action") or data.get("resolved") or data.get("fault") in ("network", "blocked"):
                continue  # nothing to check for fixes, recoveries, network notices or blocked attacks
            token = (event["session"], event["seq"])
            if token in self.acked:
                continue
            self.acked.add(token)
            fine = self.security == "normal" and self.readings_ok and not self.target_overridden
            security = data.get("security")
            if data.get("fault") == "target":
                # Has someone changed our target too? If so, the attack is probably store-wide.
                fine = not self.target_overridden
                verdict = "target_checked"
                advice = (f"Reset {origin}'s target to its safe setting and check who changed it" if fine
                          else "Check the central management system, as more than one fridge's target was changed")
            elif security in ("suspicious", "protective"):
                if origin in self.distrusted_sensors:
                    continue  # already replied to this fault
                self.distrusted_sensors.add(origin)
                verdict = "distrust_sensor"
                advice = (f"Check {origin} with a handheld thermometer" if fine
                          else "Check the store's air conditioning, as more than one fridge is affected")
            elif security == "normal":
                if origin not in self.distrusted_sensors:
                    continue
                self.distrusted_sensors.discard(origin)
                verdict, advice = "trust_restored", ""
            elif data.get("rejected"):
                verdict, advice = "noted", f"Check who can send commands to {origin}"
            elif data.get("reason") == "Door opened":
                verdict, advice = "noted", f"Make sure {origin}'s door is shut and nothing is blocking it"
            else:
                verdict, advice = "noted", ""
            await self.node.emit("ack", {
                "to": origin, "for_session": event["session"], "for_seq": event["seq"],
                "fine": fine, "my_temp": round(estimate, 1), "my_security": self.security,
                "my_target_ok": not self.target_overridden, "verdict": verdict, "advice": advice,
                "note": "My readings are normal" if fine else "I'm seeing problems too",
            })

    def snapshot(self):

        return {**self.last, "paused": self.paused, "history": list(self.history)}
