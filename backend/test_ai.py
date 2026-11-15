"""Fast checks for the AI integration. No Gemini calls and no fridge processes."""
import json
import os
import unittest
from unittest.mock import patch

from backend.ai_context import build_packet, mentioned_fridge
from backend.ai_incidents import IncidentWatcher

STATE = {
    "id": "F2", "neighbours": ["F1", "F3"], "connected": ["F1"], "members": ["F1", "F2", "F3"],
    "discovery_online": True, "event_count": 120, "rejected": 0,
    "events": [
        {"origin": "F2", "kind": "alert", "seq": 4, "data": {"security": "suspicious", "reason": "Sensor/model mismatch"}},
        {"origin": "F1", "kind": "telemetry", "seq": 9, "data": {}},
        {"origin": "F2", "kind": "rescue_request", "seq": 5, "data": {"litres": 35, "frozen": False}},
    ],
    "local": {"target": 3.0, "frozen": False, "reported": 2.0, "model_estimate": 6.1, "sensor_gap": 4.1,
              "estimate": 6.1, "actual": 6.3, "spoofed": True, "wearing": False, "security": "suspicious",
              "health": "failing", "rescue": "at_risk", "history": [{"t": 1, "reported": 2.0, "estimate": 6.1, "actual": 6.3}]},
}


class PacketTest(unittest.TestCase):
    def test_packet_hides_ground_truth_and_keeps_evidence(self):
        packet = build_packet(STATE)
        dump = json.dumps(packet)
        for secret in ('"actual"', '"spoofed"', '"wearing"'):
            self.assertNotIn(secret, dump)
        self.assertEqual(packet["fridge"]["independent_model_estimate_c"], 6.1)
        self.assertEqual([e["kind"] for e in packet["events"]], ["alert", "rescue_request"])  # no telemetry
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
        self.watcher.observe({"F2": {"local": {"security": "normal", "health": "normal"}}})
        self.assertIsNotNone(self.watcher.incidents[0]["resolved_at"])

    @patch.dict(os.environ, {"GEMINI_API_KEY": ""})
    async def test_no_key_means_no_calls(self):
        self.watcher.observe({"F2": STATE})
        await self.watcher.step()
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
