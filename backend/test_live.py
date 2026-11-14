"""End-to-end gateway + separate fridge processes + dashboard WebSocket."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import unittest

from websockets.asyncio.client import connect

from backend.distributed import read, send
from backend.test_distributed import available_ports


class LiveTest(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_stream_controls_and_dynamic_membership(self):
        port, discovery_port = available_ports(2)
        env = {**os.environ, "FRIDGE_COUNT": "2", "FRIDGE_DISCOVERY_PORT": str(discovery_port)}
        root = Path(__file__).resolve().parents[1]
        process = subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.live:app",
                                    "--host", "127.0.0.1", "--port", str(port)],
                                   cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ws = None
        ids = []
        try:
            async def ready():
                nonlocal ws
                while ws is None:
                    if process.poll() is not None:
                        self.fail("Gateway failed to start")
                    try:
                        ws = await connect(f"ws://127.0.0.1:{port}/ws", proxy=None)
                    except OSError:
                        await asyncio.sleep(.1)
            await asyncio.wait_for(ready(), 20)

            async def until(predicate):
                async def receive():
                    while True:
                        msg = read(await ws.recv())
                        if msg.get("type") == "error":
                            self.fail(msg["message"])
                        if predicate(msg):
                            return msg
                return await asyncio.wait_for(receive(), 15)

            first = await until(lambda m: m.get("type") == "state" and len(m["nodes"]) == 2
                                and all(n["local"] and n["connected"] for n in m["nodes"].values()))
            ids = list(first["nodes"])
            self.assertTrue(first["links"])
            self.assertTrue(all(n["local"]["history"] for n in first["nodes"].values()))
            await send(ws, {"command": "spoof_sensor", "id": ids[0]})
            await until(lambda m: m.get("type") == "result")
            await until(lambda m: m.get("type") == "state" and m["nodes"][ids[0]]["local"]["spoofed"])
            await send(ws, {"command": "bad_setpoint", "id": ids[0]})
            result = await until(lambda m: m.get("type") == "result")
            self.assertTrue(result["result"]["rejected"])
            await send(ws, {"command": "add"})
            result = await until(lambda m: m.get("type") == "result")
            added = result["result"]["id"]
            ids.append(added)
            await until(lambda m: m.get("type") == "state" and len(m["nodes"]) == 3
                        and all(len(n["connected"]) == 2 for n in m["nodes"].values()))
            # Requests reach peers over gossip and healthy peers emit capacity offers.
            await send(ws, {"command": "open_door", "id": ids[0]})
            await until(lambda m: m.get("type") == "result")
            await until(lambda m: m.get("type") == "state"
                        and any(e["kind"] == "capacity_offer" for e in m["events"]))
            await send(ws, {"command": "remove", "id": added})
            await until(lambda m: m.get("type") == "result")
            ids.remove(added)
            await until(lambda m: m.get("type") == "state" and added not in m["nodes"]
                        and len(m["nodes"]) == 2)
        finally:
            if ws is not None:
                # Stop child fridges explicitly before terminating the test gateway.
                for fid in ids:
                    try:
                        await send(ws, {"command": "remove", "id": fid})
                        async def ack():
                            while read(await ws.recv()).get("type") != "result":
                                pass
                        await asyncio.wait_for(ack(), 8)
                    except Exception:
                        pass
                await ws.close()
            if process.poll() is None:
                process.terminate()
            await asyncio.to_thread(process.wait, timeout=5)


if __name__ == "__main__":
    unittest.main()
