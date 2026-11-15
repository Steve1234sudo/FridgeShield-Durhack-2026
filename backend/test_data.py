"""Checks every recorded data file in backend/data (fridge_data_001..033.csv).

After adding data files, run:  python -m unittest backend.test_data
"""
import random
import unittest

from backend.csv_data import DATA_DIR, CsvCabinet
from backend.edge import EdgeController


class LocalNode:
    def __init__(self, fid="F1"):
        self.fid, self.events, self.peers, self.emitted = fid, {}, {}, []

    async def emit(self, kind, data):
        self.emitted.append((kind, data))


FILES = sorted(DATA_DIR.glob("fridge_data_*.csv")) if DATA_DIR.exists() else []


@unittest.skipUnless(FILES, "no data files in backend/data")
class DataFileTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_false_alarms_over_a_full_replay(self):
        for path in FILES:
            with self.subTest(file=path.name):
                node = LocalNode()
                cabinet = CsvCabinet(path, random.Random(1))
                edge = EdgeController(node, plant=cabinet)
                for _ in range(len(cabinet.rows) + 10):
                    await edge.tick()
                alerts = [d for kind, d in node.emitted if kind == "alert" and d.get("security")]
                self.assertEqual(alerts, [], f"{path.name} raised alarms during normal running")

    async def test_spoofing_is_caught_and_cleared(self):
        for path in FILES:
            with self.subTest(file=path.name):
                edge = EdgeController(LocalNode(), plant=CsvCabinet(path, random.Random(2)))
                for _ in range(20):
                    await edge.tick()
                edge.command("spoof_sensor")
                caught = None
                for tick in range(60):
                    await edge.tick()
                    if edge.security == "protective":
                        caught = tick
                        break
                self.assertIsNotNone(caught, f"{path.name}: spoofing wasn't caught")
                edge.command("spoof_sensor")
                for _ in range(60):
                    await edge.tick()
                self.assertEqual(edge.security, "normal", f"{path.name}: didn't trust the sensor again")


if __name__ == "__main__":
    unittest.main()
