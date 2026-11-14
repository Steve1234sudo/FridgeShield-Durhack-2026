"""Dashboard gateway and local demo launcher: python -m backend.live."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from .distributed import Discovery, SOCKET_OPTIONS, read, send


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Gateway:
    def __init__(self, discovery_port=9000):
        self.discovery = Discovery()
        self.discovery_port = discovery_port
        self.children = {}
        self.counter = 0
        self.lock = asyncio.Lock()
        self.running = True

    async def add(self):
        async with self.lock:
            if len(self.children) >= 32:
                raise ValueError("Local demo limit is 32 managed fridges")
            while True:
                self.counter += 1
                fid = f"F{self.counter}"
                if fid not in self.discovery.members and fid not in self.children:
                    break
            port = free_port()
            process = subprocess.Popen(
                [sys.executable, "-m", "backend.distributed", "node", "--id", fid,
                 "--port", str(port), "--discovery", f"ws://127.0.0.1:{self.discovery_port}", "--simulate"],
                cwd=Path(__file__).resolve().parents[1], env=os.environ.copy(),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            self.children[fid] = process
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        raise ValueError(f"{fid} failed to start")
                    if fid in self.discovery.members:
                        return fid
                    await asyncio.sleep(.1)
                raise ValueError(f"{fid} didn't register within 10 seconds")
            except BaseException:
                await self.remove(fid)
                raise

    async def remove(self, fid):
        process = self.children.pop(fid, None)
        if process is None:
            raise ValueError("Only fridges launched by this gateway can be removed here")
        if process.poll() is None:
            process.terminate()
        await asyncio.to_thread(process.wait, timeout=5)

    async def request(self, fid, message):
        member = self.discovery.members.get(fid)
        if member is None:
            raise ValueError("Fridge is offline")
        endpoint = member[1]["endpoint"].rsplit("/", 1)[0] + "/control"
        async with connect(endpoint, proxy=None, open_timeout=2, **SOCKET_OPTIONS) as ws:
            await send(ws, message)
            return read(await asyncio.wait_for(ws.recv(), 3))

    async def snapshot(self):
        ids = sorted(self.discovery.members)

        async def inspect(fid):
            try:
                response = await self.request(fid, {"type": "state", "compact": True})
                return fid, response["state"]
            except Exception:
                return fid, None

        results = await asyncio.gather(*(inspect(fid) for fid in ids))
        nodes = {fid: state for fid, state in results if state is not None}
        edges = sorted({tuple(sorted((fid, peer))) for fid, state in nodes.items()
                        for peer in state["connected"] if peer in nodes})
        events = {}
        for state in nodes.values():
            for event in state["events"]:
                events[f"{event['session']}:{event['seq']}"] = event
        return {"type": "state", "timestamp": time.time(), "nodes": nodes,
                "unreachable": [fid for fid, state in results if state is None],
                "links": edges, "managed": list(self.children), "events": list(events.values())[-300:],
                "source": "Distributed Python nodes · simulated sensors"}

    async def command(self, message):
        command = message.get("command")
        if command == "add":
            return {"id": await self.add()}
        if command == "remove":
            await self.remove(message["id"])
            return {"ok": True}
        if command in {"spoof_sensor", "open_door", "wear", "bad_setpoint", "pause"}:
            return await self.request(message["id"], {"type": "command", "command": command})
        raise ValueError("Unknown command")

    async def stop(self):
        for fid in list(self.children):
            await self.remove(fid)


gateway = Gateway(int(os.getenv("FRIDGE_DISCOVERY_PORT", "9000")))


@asynccontextmanager
async def lifespan(app):
    async with serve(gateway.discovery.handler, "127.0.0.1", gateway.discovery_port,
                     origins=[None], **SOCKET_OPTIONS):
        try:
            for _ in range(int(os.getenv("FRIDGE_COUNT", "4"))):
                await gateway.add()
            yield
        finally:
            await gateway.stop()


app = FastAPI(title="FridgeShield Live Gateway", lifespan=lifespan)


@app.get("/api/state")
async def state():
    return await gateway.snapshot()


@app.websocket("/ws")
async def dashboard(ws: WebSocket):
    origin = ws.headers.get("origin")
    allowed = os.getenv("FRIDGE_UI_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
    if origin and origin not in allowed:
        await ws.close(code=1008)
        return
    await ws.accept()
    send_lock = asyncio.Lock()

    async def reply(value):
        async with send_lock:
            await asyncio.wait_for(ws.send_json(value), 5)

    async def stream():
        while True:
            await reply(await gateway.snapshot())
            await asyncio.sleep(.7)

    task = asyncio.create_task(stream())
    try:
        while True:
            message = await ws.receive_json()
            try:
                if not isinstance(message, dict):
                    raise ValueError("Expected a command object")
                result = await gateway.command(message)
                await reply({"type": "result", "requestId": message.get("requestId"), "result": result})
            except (ValueError, KeyError, OSError, TimeoutError) as exc:
                await reply({"type": "error", "message": str(exc)})
    except WebSocketDisconnect:
        pass
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
