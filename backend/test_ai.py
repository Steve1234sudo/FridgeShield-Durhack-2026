"""Fast checks for the AI integration. No Gemini calls and no fridge processes."""
import os
os.environ["FRIDGE_USE_DATA"] = "0"  # these tests check the simulator; test_data.py covers real data
import json
import os
import unittest
from unittest.mock import patch

from backend.ai_context import build_packet, mentioned_fridge
from backend.ai_incidents import IncidentWatcher
from backend.distributed import Discovery
from backend.edge import DOOR_AUTO_CLOSE_MINUTES, SAFE_MARGIN_C, EdgeController
from backend.feed import build_feed
from backend.investigator import Action, Diagnosis, Hypothesis, tidy

STATE = {
    "id": "F2", "neighbours": ["F1", "F3"], "connected": ["F1"], "members": ["F1", "F2", "F3"],
    "discovery_online": True, "event_count": 120, "rejected": 0,
    "events": [
        {"origin": "F2", "kind": "alert", "seq": 4, "data": {"security": "suspicious", "reason": "Sensor/model mismatch"}},
        {"origin": "F1", "kind": "telemetry", "seq": 9, "data": {}},
        {"origin": "F3", "kind": "ack", "seq": 5, "data": {"to": "F2", "fine": True, "verdict": "distrust_sensor",
                                                          "advice": "Check F2 with a handheld thermometer"}},
    ],
    "local": {"target": 3.0, "frozen": False, "reported": 2.0, "model_estimate": 6.1, "sensor_gap": 4.1,
              "estimate": 6.1, "actual": 6.3, "spoofed": True, "security": "suspicious",
              "history": [{"t": 1, "reported": 2.0, "estimate": 6.1, "actual": 6.3}]},
}


class PacketTest(unittest.TestCase):
    def test_packet_hides_ground_truth_and_keeps_evidence(self):
        packet = build_packet(STATE)
        dump = json.dumps(packet)
        for secret in ('"actual"', '"spoofed"'):
            self.assertNotIn(secret, dump)
        self.assertEqual(packet["fridge"]["independent_model_estimate_c"], 6.1)
        self.assertEqual([e["kind"] for e in packet["events"]], ["alert", "ack"])  # no telemetry
        self.assertIn("stopped trusting F2's temperature readings", packet["events"][1]["text"])
        self.assertEqual(packet["network"]["neighbours_unreachable"], ["F3"])

    def test_question_can_name_another_fridge(self):
        self.assertEqual(mentioned_fridge("Is fridge 3 really overheating?", {"F1", "F3"}), "F3")
        self.assertEqual(mentioned_fridge("What about F9?", {"F1"}), None)


class WatcherTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 0.0
        self.calls = []

        async def fetch(fid):
            return STATE

        async def ask(question, packet):
            self.calls.append(question)
            return {"diagnosis": None, "error": None}

        self.watcher = IncidentWatcher(fetch, ask=ask, clock=lambda: self.now)

    @patch.dict(os.environ, {"GEMINI_API_KEY": "x", "AI_MIN_GAP": "10", "AI_INCIDENT_COOLDOWN": "60"})
    async def test_one_explanation_per_incident_with_rate_limits(self):
        self.watcher.observe({"F2": STATE})
        await self.watcher.step()
        self.watcher.observe({"F2": STATE})  # same problem again: no new call
        self.now = 100
        await self.watcher.step()
        self.assertEqual(len(self.calls), 1)
        self.watcher.observe({"F2": {"local": {"security": "normal"}}})
        self.assertIsNotNone(self.watcher.incidents[0]["resolved_at"])

    @patch.dict(os.environ, {"GEMINI_API_KEY": ""})
    async def test_no_key_means_no_calls(self):
        self.watcher.observe({"F2": STATE})
        await self.watcher.step()
        self.assertEqual(self.calls, [])


class LocalNode:
    def __init__(self, fid="F1"):
        self.fid, self.events, self.emitted = fid, {}, []

    async def emit(self, kind, data):
        self.emitted.append((kind, data))


class ActionTest(unittest.IsolatedAsyncioTestCase):
    def test_answers_are_trimmed_and_only_allowed_actions_survive(self):
        d = tidy(Diagnosis(
            summary="s", unknowns=["a", "b"], recommended_actions=["1", "2", "3"],
            hypotheses=[Hypothesis(cause=c, likelihood="High", supporting_events=[], contradicting_events=[]) for c in "xyz"],
            actions=[Action(command="close_door", reason="r"), Action(command="set_temperature_20", reason="r"),
                     Action(command="close_door", reason="again")]))
        self.assertEqual((len(d.hypotheses), len(d.recommended_actions), len(d.unknowns)), (2, 2, 1))
        self.assertEqual(d.hypotheses[0].likelihood, "high")
        self.assertEqual([a.command for a in d.actions], ["close_door"])

    async def test_remote_close_door_is_done_and_logged_by_the_fridge(self):
        node = LocalNode()
        edge = EdgeController(node)
        edge.command("open_door")
        await edge.tick()
        result = edge.command("close_door")
        self.assertTrue(result["ok"])
        await edge.tick()
        self.assertFalse(edge.plant.door_open)
        alerts = [d for kind, d in node.emitted if kind == "alert"]
        self.assertTrue(any(d.get("action") == "close_door" for d in alerts))
        with self.assertRaises(ValueError):
            edge.command("set_temperature_20")  # the fridge refuses anything it doesn't know

    async def test_door_closes_itself_with_no_network_or_ai(self):
        node = LocalNode()
        edge = EdgeController(node)
        edge.command("open_door")
        for _ in range(DOOR_AUTO_CLOSE_MINUTES + 1):
            await edge.tick()
        self.assertFalse(edge.plant.door_open)
        self.assertTrue(any(d.get("action") == "auto_close_door" for _, d in node.emitted))

    async def test_protective_mode_on_request(self):
        edge = EdgeController(LocalNode())
        edge.command("spoof_sensor")
        edge.command("protective_mode")
        await edge.tick()
        self.assertEqual(edge.security, "protective")
        self.assertEqual(edge.last["control_source"], "model")


class NeighbourTest(unittest.IsolatedAsyncioTestCase):
    async def test_neighbour_checks_itself_and_replies_once(self):
        node = LocalNode("F3")
        node.peers = {"F2": None}
        node.events = {"s2:4": {"origin": "F2", "session": "s2", "seq": 4, "kind": "alert",
                                "data": {"security": "suspicious", "reason": "Sensor/model mismatch"}}}
        edge = EdgeController(node)
        await edge.tick()
        await edge.tick()
        acks = [d for kind, d in node.emitted if kind == "ack"]
        self.assertEqual(len(acks), 1)
        self.assertEqual((acks[0]["to"], acks[0]["for_seq"], acks[0]["verdict"]), ("F2", 4, "distrust_sensor"))
        self.assertEqual(acks[0]["advice"], "Check F2 with a handheld thermometer")
        self.assertEqual(edge.last["distrusted_sensors"], ["F2"])
        # When F2 says its sensor is fine again, F3 trusts its readings again
        node.events["s2:5"] = {"origin": "F2", "session": "s2", "seq": 5, "kind": "alert",
                               "data": {"security": "normal", "reason": "Sensor agrees again"}}
        await edge.tick()
        await edge.tick()
        self.assertEqual(edge.last["distrusted_sensors"], [])
        self.assertEqual([d["verdict"] for k, d in node.emitted if k == "ack"][-1], "trust_restored")

    def test_feed_shows_direct_and_relayed_delivery_and_replies(self):
        alert = {"origin": "F2", "session": "s2", "seq": 4, "kind": "alert", "data": {"security": "suspicious"}}
        ack = {"origin": "F3", "session": "s3", "seq": 9, "kind": "ack",
               "data": {"to": "F2", "for_session": "s2", "for_seq": 4, "fine": True, "verdict": "distrust_sensor",
                        "advice": "Check F2 with a handheld thermometer"}}
        nodes = {
            "F2": {"events": [alert, ack], "arrivals": [{"event": "s2:4", "via": None, "at": 100.0},
                                                       {"event": "s3:9", "via": "F3", "at": 101.2}]},
            "F3": {"events": [alert, ack], "arrivals": [{"event": "s2:4", "via": "F2", "at": 100.1},
                                                       {"event": "s3:9", "via": None, "at": 101.0}]},
            "F4": {"events": [alert], "arrivals": [{"event": "s2:4", "via": "F3", "at": 100.3}]},
        }
        [item] = build_feed(nodes)
        self.assertEqual(item["text"], "F2 reported a possible sensor fault to F3.")
        self.assertEqual(item["outcome"], "F3 stopped trusting F2's temperature readings.")
        self.assertEqual(item["advice"], "Advice: Check F2 with a handheld thermometer.")
        self.assertEqual([(h["fridge"], h["direct"], h["after_ms"]) for h in item["heard_by"]],
                         [("F3", True, 100), ("F4", False, 300)])
        self.assertEqual(item["replies"][0]["fridge"], "F3")


class TopologyTest(unittest.TestCase):
    def test_random_network_keeps_every_fridge_at_1_to_3_neighbours_and_connected(self):
        for seed in range(200):
            d = Discovery(topology="random")
            d.rng.seed(seed)
            for i in range(1, 13):  # fridges join one by one
                d.members[f"F{i}"] = None
                d.attach(f"F{i}")
            for gone in ("F3", "F7", "F1"):  # and some leave
                del d.members[gone]
                d.detach(gone)
            degrees = {m: d.degree(m) for m in d.members}
            self.assertTrue(all(1 <= deg <= 3 for deg in degrees.values()), (seed, degrees))
            self.assertEqual(len(d.components()), 1, seed)

    def test_not_always_a_cycle(self):
        shapes = set()
        for seed in range(20):
            d = Discovery(topology="random")
            d.rng.seed(seed)
            for i in range(1, 7):
                d.members[f"F{i}"] = None
                d.attach(f"F{i}")
            shapes.add(frozenset(d.edges))
        self.assertGreater(len(shapes), 10)


class TargetTest(unittest.IsolatedAsyncioTestCase):
    async def test_override_is_spotted_and_the_fridge_restores_it_before_the_limit(self):
        node = LocalNode()
        edge = EdgeController(node)
        edge.command("override_target")
        peak = 0
        for _ in range(60):
            await edge.tick()
            peak = max(peak, edge.last["estimate"])
            if not edge.last["target_overridden"]:
                break
        self.assertFalse(edge.last["target_overridden"])
        self.assertLess(peak, edge.safe_target + SAFE_MARGIN_C)
        await edge.tick()
        alerts = [d for k, d in node.emitted if k == "alert"]
        self.assertEqual(alerts[0]["fault"], "target")
        self.assertTrue(any(d.get("action") == "auto_restore_target" for d in alerts))

    async def test_restore_target_on_request(self):
        edge = EdgeController(LocalNode())
        edge.command("override_target")
        await edge.tick()
        self.assertTrue(edge.command("restore_target")["ok"])
        self.assertEqual(edge.plant.target, edge.safe_target)

    async def test_neighbours_check_their_own_target(self):
        node = LocalNode("F3")
        node.peers = {"F2": None}
        node.events = {"s2:4": {"origin": "F2", "session": "s2", "seq": 4, "kind": "alert",
                                "data": {"fault": "target", "target": 12, "reason": "Target changed"}}}
        edge = EdgeController(node)
        await edge.tick()
        [ack] = [d for k, d in node.emitted if k == "ack"]
        self.assertEqual((ack["verdict"], ack["fine"]), ("target_checked", True))
        self.assertIn("check who changed it", ack["advice"])


class SafeModeTest(unittest.IsolatedAsyncioTestCase):
    async def test_cut_off_fridge_restores_its_target_and_only_blocks_new_attacks(self):
        edge = EdgeController(LocalNode())
        edge.command("override_target")
        await edge.tick()
        edge.enter_safe_mode()
        self.assertEqual(edge.plant.target, edge.safe_target)
        await edge.tick()
        self.assertTrue(any(d.get("action") == "safe_mode_restore" for _, d in edge.node.emitted))
        for attack in ("spoof_sensor", "override_target", "bad_setpoint"):
            self.assertTrue(edge.starts_attack(attack))
        self.assertFalse(edge.starts_attack("open_door"))  # physical, not over the network
        edge.command("spoof_sensor")
        self.assertFalse(edge.starts_attack("spoof_sensor"))  # undoing an attack is allowed

    def test_feed_names_the_blocked_attack(self):
        alert = {"origin": "F1", "session": "s", "seq": 1, "kind": "alert",
                 "data": {"fault": "blocked", "command": "override_target"}}
        nodes = {"F1": {"events": [alert], "arrivals": [{"event": "s:1", "via": None, "at": 1.0}]},
                 "F2": {"events": [alert], "arrivals": [{"event": "s:1", "via": "F1", "at": 9.0}]}}
        self.assertEqual(build_feed(nodes)[0]["text"],
                         "F1 blocked a target override while it was cut off, and told F2.")


if __name__ == "__main__":
    unittest.main()
