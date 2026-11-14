"""Real-socket tests, including a cluster of independent Python processes."""
import asyncio
import copy
import os
from pathlib import Path
import socket
import subprocess
import sys
import unittest

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from backend.distributed import Discovery, Fridge, SOCKET_OPTIONS, canonical, read, send


async def eventually(predicate, timeout=12):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.05)
    await asyncio.wait_for(wait(), timeout)


class MeshTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.discovery = Discovery()
        self.server = await serve(self.discovery.handler, "127.0.0.1", 0, **SOCKET_OPTIONS)
        self.discovery_url = f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        self.nodes = {}

    async def add_node(self, fid):
        node = Fridge(fid, "", self.discovery_url)
        server = await serve(node.handler, "127.0.0.1", 0, **SOCKET_OPTIONS)
        node.endpoint = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/peer"
        tasks = [asyncio.create_task(node.discover()), asyncio.create_task(node.reconcile())]
        self.nodes[fid] = (node, server, tasks)
        await eventually(lambda: fid in self.discovery.members)
        return node

    async def remove_node(self, fid):
        node, server, tasks = self.nodes.pop(fid)
        tasks += list(node.diallers.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for ws, _ in list(node.peers.values()):
            await ws.close()
        server.close()
        await server.wait_closed()
        await eventually(lambda: fid not in self.discovery.members)

    async def asyncTearDown(self):
        for fid in list(self.nodes):
            await self.remove_node(fid)
        self.server.close()
        await self.server.wait_closed()

    async def test_multihop_join_leave_restart_and_discovery_outage(self):
        nodes = [await self.add_node(fid) for fid in ("F1", "F2", "F3", "F4")]
        await eventually(lambda: all(len(n.peers) == 2 for n in nodes))
        # F1 and F3 are not direct neighbours in the four-node ring.
        self.assertNotIn("F3", nodes[0].neighbours)
        await nodes[0].emit("rescue_request", {"litres": 20})
        await eventually(lambda: all(len(n.events) == 1 for n in nodes))
        joined = await self.add_node("F5")
        await eventually(lambda: len(joined.events) == 1)
        # Remove a member: discovery repairs the ring and events still propagate.
        old_session = nodes[1].session
        await self.remove_node("F2")
        survivors = [nodes[0], nodes[2], nodes[3], joined]
        await eventually(lambda: all(len(n.peers) == 2 and "F2" not in n.neighbours for n in survivors))
        await joined.emit("capacity_offer", {"for": "F1", "litres": 20})
        await eventually(lambda: all(len(n.events) == 2 for n in survivors))
        restarted = await self.add_node("F2")
        self.assertNotEqual(old_session, restarted.session)
        await restarted.emit("telemetry", {"temperature": 3})
        await eventually(lambda: all(len(n.events) == 3 for n in [*survivors, restarted]))
        # Discovery outage doesn't tear down established neighbour sockets.
        self.server.close()
        await self.server.wait_closed()
        await restarted.emit("alert", {"reason": "door open"})
        await eventually(lambda: all(len(n.events) == 4 for n in [*survivors, restarted]))

    async def test_reconnect_and_out_of_order_holes(self):
        a, b = await self.add_node("A"), await self.add_node("B")
        await eventually(lambda: "B" in a.peers)
        first = await a.emit("telemetry", {"temperature": 3})
        second = await a.emit("telemetry", {"temperature": 4})
        # Deliver a later event first. Maximum clock alone must not hide the gap.
        self.assertTrue(b.ingest(second))
        self.assertNotIn(f"{a.session}:1", b.events)
        await eventually(lambda: len(b.events) == 2)
        await a.peers["B"][0].close()
        await a.emit("alert", {"reason": "offline test"})
        await eventually(lambda: len(b.events) == 3 and "B" in a.peers)
        self.assertFalse(b.ingest(first))  # Duplicate doesn't grow the log.

    async def test_tampered_and_conflicting_events(self):
        a, b = await self.add_node("A"), await self.add_node("B")
        await eventually(lambda: a.session in b.keys)
        event = await a.emit("telemetry", {"temperature": 3})
        tampered = copy.deepcopy(event)
        tampered["data"]["temperature"] = 90
        self.assertFalse(b.ingest(tampered))
        self.assertEqual(b.rejected, 1)
        self.assertTrue(b.ingest(event))
        conflicting = copy.deepcopy(event)
        conflicting["data"] = {"temperature": 9}
        body = {k: conflicting[k] for k in ("origin", "session", "seq", "kind", "data")}
        conflicting["signature"] = a.private_key.sign(canonical(body)).hex()
        self.assertFalse(b.ingest(conflicting))
        self.assertEqual(b.rejected, 2)


def available_ports(count):
    sockets = []
    try:
        for _ in range(count):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


class ProcessTest(unittest.IsolatedAsyncioTestCase):
    async def test_three_independent_processes(self):
        ports = available_ports(4)
        processes = []
        root = Path(__file__).resolve().parents[1]

        async def state(port):
            async with connect(f"ws://127.0.0.1:{port}/control", proxy=None) as ws:
                await send(ws, {"type": "state"})
                return read(await ws.recv())["state"]

        def spawn(*args):
            processes.append(subprocess.Popen(
                [sys.executable, "-m", "backend.distributed", *args], cwd=root,
                env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

        try:
            spawn("discovery", "--port", str(ports[0]))
            for index, port in enumerate(ports[1:]):
                spawn("node", "--id", f"P{index}", "--port", str(port),
                      "--discovery", f"ws://127.0.0.1:{ports[0]}")
            async def ready():
                while True:
                    try:
                        states = [await state(port) for port in ports[1:]]
                        if all(len(s["connected"]) == 2 for s in states):
                            return
                    except Exception as exc:
                        if any(p.poll() is not None for p in processes):
                            raise AssertionError("A child process exited") from exc
                    await asyncio.sleep(0.1)
            await asyncio.wait_for(ready(), 15)
            async with connect(f"ws://127.0.0.1:{ports[1]}/control", proxy=None) as ws:
                await send(ws, {"type": "emit", "kind": "rescue_request", "data": {"litres": 12}})
                self.assertEqual(read(await ws.recv())["type"], "emitted")
            async def converged():
                while True:
                    states = [await state(port) for port in ports[1:]]
                    if all(len(s["events"]) == 1 for s in states):
                        return
                    await asyncio.sleep(0.1)
            await asyncio.wait_for(converged(), 8)
            # Kill a process rather than issuing a graceful membership leave.
            processes[-1].terminate()
            await asyncio.to_thread(processes[-1].wait, timeout=5)
            async def shrunk():
                while True:
                    states = [await state(port) for port in ports[1:3]]
                    if all(len(s["connected"]) == 1 and "P2" not in s["members"] for s in states):
                        return
                    await asyncio.sleep(0.1)
            await asyncio.wait_for(shrunk(), 12)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                await asyncio.to_thread(process.wait, timeout=5)


if __name__ == "__main__":
    unittest.main()
