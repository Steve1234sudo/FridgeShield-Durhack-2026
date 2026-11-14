"""Local fridge behaviour. Remote state is learned only from signed events."""
import random

from .thermal import PhysicalCabinet, ThermalKalmanObserver


class EdgeController:
    def __init__(self, node, target=3.0, capacity=60, stock=35, frozen=False):
        self.node = node
        self.plant = PhysicalCabinet(node.fid, target, frozen, capacity, stock, random.Random(node.fid))
        self.observer = ThermalKalmanObserver(target, frozen)
        # Independent prediction must never fuse the sensor under investigation.
        # Otherwise a plausible-but-frozen reading drags the detector toward itself.
        self.safety_observer = ThermalKalmanObserver(target, frozen)
        self.sensor_window = []
        self.t = 0
        self.compressor = False
        self.mismatch = 0
        self.recovery = 0
        self.security = "normal"
        self.health = "normal"
        self.history = []
        self.requested = False
        self.offered = set()
        self.paused = False
        self.running = True
        self.last = {}

    def command(self, command):
        if command == "spoof_sensor":
            self.plant.spoofed = not self.plant.spoofed
        elif command == "open_door":
            self.plant.door_open = not self.plant.door_open
        elif command == "wear":
            self.plant.wearing = not self.plant.wearing
        elif command == "bad_setpoint":
            return {"rejected": True, "reason": "Unsafe remote setpoint rejected; local target retained"}
        elif command == "pause":
            self.paused = not self.paused
        else:
            raise ValueError("Unknown fridge command")
        return {"ok": True}

    async def tick(self):
        if self.paused:
            return
        self.t += 1
        p = self.plant
        p.compressor_on = self.compressor
        reported, power, door = p.step_physics()
        model_estimate, _ = self.safety_observer.update(reported, power, door, 19.0, False)
        gap = abs(reported - model_estimate)
        self.sensor_window = (self.sensor_window + [reported])[-5:]
        # A frozen spoof can briefly match the model during a cooling cycle.
        # Require both agreement and a changing sensor before trusting it again.
        sensor_changes = len(self.sensor_window) == 5 and max(self.sensor_window) - min(self.sensor_window) > .4
        self.mismatch = self.mismatch + 1 if gap > 2.5 else 0
        self.recovery = self.recovery + 1 if gap < 1.0 and sensor_changes else 0
        previous = self.security
        if self.mismatch >= 7:
            self.security = "protective"
        elif self.mismatch >= 2 and self.security != "protective":
            self.security = "suspicious"
        elif self.recovery >= 5:
            self.security = "normal"
        fused, _ = self.observer.update(reported, power, door, 19.0,
                                       self.security == "normal" and gap <= 2.5)
        estimate = model_estimate if self.security != "normal" else fused
        control = estimate if self.security == "protective" else reported
        if control > p.target + 0.8:
            self.compressor = True
        elif control < p.target - 0.8:
            self.compressor = False
        self.health = ("failing" if estimate > p.target + 3 else "door_open" if door else
                       "degrading" if self.observer.estimated_wear > .25 else "normal")
        if previous != self.security:
            await self.node.emit("alert", {
                "reason": "Sensor agrees with model again" if self.security == "normal" else "Sensor/model mismatch",
                "security": self.security})
        if self.health == "failing" and not self.requested:
            self.requested = True
            await self.node.emit("rescue_request", {"litres": p.stock, "frozen": p.frozen})
        # Respond to requests learned through gossip, never inspect a remote object.
        for event in list(self.node.events.values()):
            request = event["data"]
            token = (event["session"], event["seq"])
            spare = p.capacity - p.stock
            if (event["kind"] == "rescue_request" and event["origin"] != self.node.fid
                    and token not in self.offered and self.health == "normal" and self.security == "normal"
                    and request.get("frozen", False) == p.frozen and spare > 0):
                self.offered.add(token)
                await self.node.emit("capacity_offer", {"for": event["origin"], "litres": spare,
                                                       "request_session": event["session"],
                                                       "request_seq": event["seq"]})
        self.history.append({"t": self.t, "reported": reported, "estimate": estimate, "actual": p.true_temp})
        self.history = self.history[-60:]
        self.last = {"id": self.node.fid, "t": self.t, "target": p.target, "capacity": p.capacity,
                     "stock": p.stock, "frozen": p.frozen, "reported": reported, "estimate": estimate,
                     "actual": p.true_temp, "power_watts": power, "compressor": self.compressor,
                     "door": door, "spoofed": p.spoofed, "wearing": p.wearing,
                     "wear": self.observer.estimated_wear, "health": self.health,
                     "security": self.security, "paused": self.paused,
                     "sensor_gap": gap, "model_estimate": model_estimate,
                     "control_source": "model" if self.security == "protective" else "sensor",
                     "rescue": "at_risk" if self.requested else "none"}
        await self.node.emit("telemetry", self.last)

    def snapshot(self):
        return {**self.last, "paused": self.paused, "history": list(self.history)}
