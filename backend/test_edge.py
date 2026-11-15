import os
os.environ["FRIDGE_USE_DATA"] = "0"  # these tests check the simulator; test_data.py covers real data
import unittest

from backend.edge import EdgeController


class LocalNode:
    def __init__(self, fid):
        self.fid = fid
        self.events = {}
        self.emitted = []

    async def emit(self, kind, data):
        self.emitted.append((kind, data))


class EdgeTest(unittest.IsolatedAsyncioTestCase):
    async def test_stuck_cold_sensor_detected_and_actual_temperature_recovers(self):
        for fid in ("F1", "F2", "F3", "F4"):
            with self.subTest(fridge=fid):
                node = LocalNode(fid)
                edge = EdgeController(node)
                for _ in range(30):
                    await edge.tick()
                edge.command("spoof_sensor")
                readings = []
                for _ in range(100):
                    await edge.tick()
                    readings.append(dict(edge.last))
                self.assertTrue(any(r["security"] == "protective" for r in readings[:30]))
                self.assertTrue(any(r["compressor"] for r in readings if r["security"] == "protective"))
                self.assertLess(edge.last["actual"], edge.last["target"] + 2)
                self.assertGreater(max(r["actual"] for r in readings), edge.last["actual"] + 1)
                self.assertTrue(all(abs(r["reported"] - 2) < .11 for r in readings))
                self.assertEqual(edge.last["control_source"], "model")
                edge.command("spoof_sensor")
                for _ in range(30):
                    await edge.tick()
                self.assertEqual(edge.security, "normal")

    async def test_healthy_sensor_is_not_flagged_after_long_run(self):
        edge = EdgeController(LocalNode("healthy"))
        for _ in range(200):
            await edge.tick()
            self.assertEqual(edge.security, "normal")


if __name__ == "__main__":
    unittest.main()
