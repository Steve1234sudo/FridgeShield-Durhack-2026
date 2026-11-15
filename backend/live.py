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
import uuid

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from .distributed import Discovery, SOCKET_OPTIONS, read, send
from .investigator_api import router as investigator_router
from pydantic import BaseModel

from .ai_context import build_packet, mentioned_fridge
from .ai_incidents import IncidentWatcher
from .feed import active_links, build_feed
from .report import FridgeHistory, gather, markdown, write_up
from .investigator import ALLOWED_ACTIONS, investigate


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
                cwd=Path(__file__).resolve().parents[1], env={**os.environ, "FRIDGE_EXIT_WITH_PARENT": "1"},
                stdin=subprocess.PIPE,  # the fridge exits when this pipe closes, i.e. when the server stops
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
        # Every neighbour pair, connected or not, so the dashboard can show cut links.
        topology = sorted({tuple(sorted((fid, peer))) for fid, state in nodes.items()
                           for peer in state["neighbours"] if peer in nodes})
        events = {}
        for state in nodes.values():
            for event in state["events"]:
                events[f"{event['session']}:{event['seq']}"] = event
        return {"type": "state", "timestamp": time.time(), "nodes": nodes,
                "unreachable": [fid for fid, state in results if state is None],
                "links": edges, "topology": topology, "managed": list(self.children), "events": list(events.values())[-300:],
                "source": "Distributed Python nodes · simulated sensors",
                "feed": build_feed(nodes), "active_links": active_links(nodes), "server_id": SERVER_ID,
                "incidents": watcher.public()}

    async def command(self, message):
        command = message.get("command")
        if command == "add":
            return {"id": await self.add()}
        if command == "remove":
            await self.remove(message["id"])
            return {"ok": True}
        if command in {"spoof_sensor", "open_door", "override_target", "isolate", "bad_setpoint", "pause"}:
            return await self.request(message["id"], {"type": "command", "command": command})
        raise ValueError("Unknown command")

    async def stop(self):
        for fid in list(self.children):
            await self.remove(fid)


gateway = Gateway(int(os.getenv("FRIDGE_DISCOVERY_PORT", "9000")))
# A new ID every time the server starts. The dashboard clears its own state (AI chat,
# selection, messages) when it sees a new one, so each run starts from a clean slate.
SERVER_ID = uuid.uuid4().hex[:8]


async def fridge_view(fid):
    """Ask one fridge, over the store network, for everything it knows.

    A fridge that has been cut off refuses, so the AI genuinely can't see it.
    """
    reply = await gateway.request(fid, {"type": "state", "compact": True, "channel": "network"})
    if reply.get("type") == "error":
        raise ValueError(reply["message"])
    return reply["state"]


# Each fridge's peak temperature and time above its limit, for incident reports.
history = FridgeHistory()


async def fleet_nodes():
    nodes = (await gateway.snapshot())["nodes"]
    history.observe(nodes)
    return nodes


async def apply_ai_actions(fid, actions):
    """Send the AI's requested fixes to the fridge, which checks and carries them out itself.

    Only commands in ALLOWED_ACTIONS are ever sent, and only to the fridge the AI was asked
    about. Set AI_CAN_ACT=0 in backend/.env to make the AI suggest fixes without applying them.
    """
    if os.getenv("AI_CAN_ACT", "1") == "0":
        return [{**a, "ok": False, "applied": False, "message": "Suggested only (AI_CAN_ACT=0)"} for a in actions]
    results = []
    for action in actions:
        command = action.get("command")
        if command not in ALLOWED_ACTIONS:
            results.append({**action, "ok": False, "applied": False, "message": "Not an allowed action"})
            continue
        try:
            reply = await gateway.request(fid, {"type": "command", "command": command, "channel": "network"})
            ok = reply.get("type") == "result" and bool(reply.get("ok"))
            results.append({**action, "ok": ok, "applied": ok,
                            "message": reply.get("message") or reply.get("reason") or ""})
            if reply.get("type") == "error":
                results[-1]["message"] += ". Its own safety reflexes still apply."
        except Exception as exc:
            results.append({**action, "ok": False, "applied": False,
                            "message": f"Couldn't reach {fid} ({exc}). Its own safety reflexes still apply."})
    return results


# Explains new problems automatically, and may apply the same safe fixes.
watcher = IncidentWatcher(fridge_view, act=apply_ai_actions)


@asynccontextmanager
async def lifespan(app):
    async with serve(gateway.discovery.handler, "127.0.0.1", gateway.discovery_port,
                     origins=[None], **SOCKET_OPTIONS):
        watcher_task = None
        try:
            for _ in range(int(os.getenv("FRIDGE_COUNT", "4"))):
                await gateway.add()
            watcher_task = asyncio.create_task(watcher.run(fleet_nodes))
            yield
        finally:
            if watcher_task:
                watcher_task.cancel()
                await asyncio.gather(watcher_task, return_exceptions=True)
            await gateway.stop()


app = FastAPI(title="FridgeShield Backend", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("FRIDGE_UI_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(investigator_router)


class AskRequest(BaseModel):
    fridge_id: str
    question: str


@app.post("/api/ask")
async def ask(req: AskRequest):
    """Answer a staff question using one fridge's own view of the store.

    If the question names another fridge ("is F5 really overheating?"), that fridge is asked.
    """
    fid = mentioned_fridge(req.question, gateway.discovery.members) or req.fridge_id
    try:
        state = await fridge_view(fid)
    except Exception as exc:
        return {"fridge_id": fid, "diagnosis": None, "invalid_citations": [], "raw": "", "prompt": None,
                "model": os.getenv("GEMINI_MODEL", ""), "latency_ms": 0,
                "error": f"Couldn't reach {fid} to ask what it knows ({exc}). It keeps protecting itself, and the other fridges carry on as normal."}
    result = await investigate(req.question, build_packet(state))
    actions = (result.get("diagnosis") or {}).get("actions") or []
    if actions:
        result["actions_taken"] = await apply_ai_actions(fid, actions)
    return {"fridge_id": fid, **result}


class ReportRequest(BaseModel):
    fridge_id: str


@app.post("/api/report")
async def report(req: ReportRequest):
    """An incident report for one fridge, on demand: exact facts plus an AI-written summary."""
    snapshot = await gateway.snapshot()
    node = snapshot["nodes"].get(req.fridge_id)
    if not node:
        return {"fridge_id": req.fridge_id, "error": f"{req.fridge_id} isn't in the network", "markdown": ""}
    facts = gather(req.fridge_id, node, build_feed(snapshot["nodes"], limit=200), watcher.incidents,
                   history.stats.get(req.fridge_id, {}))
    writing, error = await write_up(facts)
    generated_at = time.time()
    return {"fridge_id": req.fridge_id, "generated_at": generated_at, "facts": facts, "writing": writing,
            "error": error, "model": os.getenv("GEMINI_MODEL", ""),
            "markdown": markdown(facts, writing, generated_at, SERVER_ID)}


@app.get("/api/incidents")
def incidents():
    """Recent automatic incident explanations, including the full prompts sent."""
    return watcher.incidents[-20:]


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
