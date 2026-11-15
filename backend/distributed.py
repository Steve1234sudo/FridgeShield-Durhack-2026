"""Independent WebSocket fridge nodes. See backend/DISTRIBUTED.md."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import random
import sys
import time
import uuid
from urllib.parse import urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from .edge import REMEDIATIONS

LOG = logging.getLogger("fridge.mesh")
SOCKET_OPTIONS = dict(ping_interval=3, ping_timeout=3, close_timeout=1, max_size=1048576)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


async def send(ws, value):
    await asyncio.wait_for(ws.send(json.dumps(value, allow_nan=False)), timeout=3)


def read(raw):
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


class Discovery:
    """Membership only: event traffic never passes through this service.

    By default fridges form a random network: each newcomer links to 1-3 random fridges
    that still have room, so every fridge has 1-3 neighbours. When a fridge leaves, any
    pieces of the network it was holding together are rejoined. Existing links are kept
    when others join or leave. topology="ring" gives the original sorted ring instead.
    Set FRIDGE_TOPOLOGY_SEED for the same random network every run (handy for demos).
    WebSocket ping/pong detects dead members.
    """
    MAX_DEGREE = 3

    def __init__(self, topology=None):
        self.members = {}
        self.keys = {}  # Keep departed session keys so old events remain verifiable.
        self.lock = asyncio.Lock()
        self.topology = topology or os.getenv("FRIDGE_TOPOLOGY", "random")
        self.edges = set()  # frozenset({a, b}) per neighbour link (random topology)
        self.rng = random.Random(os.getenv("FRIDGE_TOPOLOGY_SEED"))

    def degree(self, fid):
        return sum(1 for edge in self.edges if fid in edge)

    def attach(self, fid):
        """Link a newcomer to 1-3 random fridges that have fewer than 3 neighbours."""
        others = sorted(m for m in self.members if m != fid)
        if not others:
            return
        room = [m for m in others if self.degree(m) < self.MAX_DEGREE]
        if not room:
            # Everyone is full: splice the newcomer into a random link (u-v becomes u-new-v).
            u, v = self.rng.choice(sorted(tuple(sorted(edge)) for edge in self.edges))
            self.edges.discard(frozenset((u, v)))
            self.edges |= {frozenset((fid, u)), frozenset((fid, v))}
            return
        for peer in self.rng.sample(room, self.rng.randint(1, min(self.MAX_DEGREE, len(room)))):
            self.edges.add(frozenset((fid, peer)))

    def detach(self, fid):
        """Drop a departed fridge's links, then rejoin any pieces it was holding together."""
        self.edges = {edge for edge in self.edges if fid not in edge}
        while True:
            groups = self.components()
            if len(groups) <= 1:
                return
            ends = []
            for group in groups[:2]:
                room = [m for m in group if self.degree(m) < self.MAX_DEGREE]
                ends.append(self.rng.choice(room or [min(group, key=self.degree)]))
            self.edges.add(frozenset(ends))

    def components(self):
        unseen, groups = set(self.members), []
        while unseen:
            start = min(unseen)
            group, queue = {start}, [start]
            while queue:
                node = queue.pop()
                for edge in self.edges:
                    if node in edge:
                        (other,) = edge - {node}
                        if other not in group:
                            group.add(other)
                            queue.append(other)
            unseen -= group
            groups.append(sorted(group))
        return groups

    def neighbours_of(self, fid, ids):
        if self.topology == "ring":
            i = ids.index(fid)
            return sorted({ids[(i - 1) % len(ids)], ids[(i + 1) % len(ids)]} - {fid})
        return sorted(other for edge in self.edges if fid in edge for other in edge if other != fid)

    async def publish(self):
        ids = sorted(self.members)
        for fid in ids:
            neighbours = self.neighbours_of(fid, ids)
            ws, _ = self.members[fid]
            try:
                await send(ws, {
                    "type": "membership",
                    "members": {key: info for key, (_, info) in self.members.items()},
                    "keys": self.keys,
                    "neighbours": neighbours,
                })
            except (ConnectionClosed, TimeoutError):
                pass  # Its handler will remove it and announce the new topology.

    async def handler(self, ws):
        fid = None
        try:
            msg = read(await asyncio.wait_for(ws.recv(), 5))
            if msg.get("type") != "register":
                raise ValueError("Expected register")
            fid, endpoint, session, key = (msg[k] for k in ("id", "endpoint", "session", "key"))
            if not isinstance(fid, str) or not fid or len(fid) > 80:
                raise ValueError("Invalid fridge ID")
            parsed = urlparse(endpoint)
            if parsed.scheme not in ("ws", "wss") or not parsed.hostname or not parsed.port:
                raise ValueError("Expected a reachable ws://host:port endpoint")
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(key))
            async with self.lock:
                if fid in self.members:
                    raise ValueError("Fridge ID is already online")
                if session in self.keys and self.keys[session] != {"id": fid, "key": key}:
                    raise ValueError("Session identity changed")
                self.keys[session] = {"id": fid, "key": key}
                self.members[fid] = (ws, {"endpoint": endpoint, "session": session, "key": key})
                self.attach(fid)
                await self.publish()
            LOG.info("Joined: %s", fid)
            async for raw in ws:
                if read(raw).get("type") != "heartbeat":
                    raise ValueError("Discovery accepts membership traffic only")
        except (ValueError, KeyError, TypeError, TimeoutError) as exc:
            await ws.close(code=1008, reason=str(exc)[:100])
        except ConnectionClosed:
            pass
        finally:
            async with self.lock:
                if fid in self.members and self.members[fid][0] is ws:
                    del self.members[fid]
                    self.detach(fid)
                    await self.publish()
                    LOG.info("Left: %s", fid)


class Fridge:
    """One node's private state; other nodes can only be reached via sockets."""

    def __init__(self, fid, endpoint, discovery):
        self.fid, self.endpoint, self.discovery = fid, endpoint, discovery
        self.session = uuid.uuid4().hex
        self.private_key = Ed25519PrivateKey.generate()
        self.public_key = self.private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        self.keys = {self.session: {"id": fid, "key": self.public_key}}
        self.members = {}
        self.neighbours = set()
        self.peers = {}
        self.diallers = {}
        self.events = {}  # Grow-only set keyed by (session, sequence); not global integers.
        self.clock = {}
        self.sequence = 0
        self.discovery_online = False
        self.changed = asyncio.Event()
        self.rejected = 0
        self.edge = None
        # When cut off, the fridge refuses peer links and requests arriving over the store
        # network (from the AI). It keeps running its own safety reflexes and logging events,
        # which sync to its neighbours when it reconnects.
        self.isolated = False
        # Who told us each (non-telemetry) event, and when: proof of direct peer communication.
        self.arrivals = []

    def snapshot(self, compact=False):
        return {
            "id": self.fid, "session": self.session,
            "discovery_online": self.discovery_online,
            "neighbours": sorted(self.neighbours), "connected": sorted(self.peers),
            "members": sorted(self.members), "clock": self.clock,
            "events": ([e for e in self.events.values() if e["kind"] != "telemetry"][-100:]
                       if compact else list(self.events.values())),
            "event_count": len(self.events), "rejected": self.rejected,
            "arrivals": self.arrivals[-100:], "isolated": self.isolated,
            "local": self.edge.snapshot() if self.edge else None,
        }

    async def emit(self, kind, data):
        if kind not in {"telemetry", "alert", "rescue_request", "capacity_offer", "ack"}:
            raise ValueError("Unsupported event kind")
        if not isinstance(data, dict):
            raise ValueError("Event data must be an object")
        self.sequence += 1
        body = {"origin": self.fid, "session": self.session, "seq": self.sequence,
                "kind": kind, "data": data}
        event = {**body, "signature": self.private_key.sign(canonical(body)).hex()}
        self.ingest(event)
        return event

    def ingest(self, event, via=None):
        """Store a verified event. `via` is the neighbour that delivered it (None if we created it)."""
        try:
            body = {k: event[k] for k in ("origin", "session", "seq", "kind", "data")}
            identity = self.keys[body["session"]]
            if body["origin"] != identity["id"] or type(body["seq"]) is not int or body["seq"] < 1:
                raise ValueError("Invalid event identity or sequence")
            if body["kind"] not in {"telemetry", "alert", "rescue_request", "capacity_offer", "ack"} or not isinstance(body["data"], dict):
                raise ValueError("Invalid event payload")
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(identity["key"])).verify(
                bytes.fromhex(event["signature"]), canonical(body))
            eid = f"{body['session']}:{body['seq']}"
            if eid in self.events:
                if canonical(self.events[eid]) != canonical(event):
                    LOG.warning("Conflicting signed event from %s at %s", body["origin"], eid)
                    self.rejected += 1
                return False
            self.events[eid] = event
            self.clock[body["session"]] = max(self.clock.get(body["session"], 0), body["seq"])
            if body["kind"] != "telemetry":
                self.arrivals = (self.arrivals + [{"event": eid, "origin": body["origin"], "kind": body["kind"],
                                                   "via": via, "at": time.time()}])[-200:]
            self.changed.set()
            LOG.info("%s received %s from %s (#%s)", self.fid, body["kind"], body["origin"], body["seq"])
            return True
        except (KeyError, TypeError, ValueError, InvalidSignature):
            self.rejected += 1
            return False

    async def discover(self):
        while True:
            try:
                async with connect(self.discovery, proxy=None, **SOCKET_OPTIONS) as ws:
                    await send(ws, {"type": "register", "id": self.fid, "endpoint": self.endpoint,
                                    "session": self.session, "key": self.public_key})
                    async for raw in ws:
                        msg = read(raw)
                        if msg.get("type") != "membership":
                            continue
                        self.keys.update(msg["keys"])
                        self.members = msg["members"]
                        self.neighbours = set(msg["neighbours"])
                        self.discovery_online = True
                        # Drop obsolete edges or connections to an old incarnation.
                        for peer, (sock, session) in list(self.peers.items()):
                            if peer not in self.neighbours or self.members[peer]["session"] != session:
                                await sock.close()
                        for peer, task in list(self.diallers.items()):
                            if peer not in self.neighbours:
                                task.cancel()
                                await asyncio.gather(task, return_exceptions=True)
                                self.diallers.pop(peer, None)
                        for peer in self.neighbours:
                            # Exactly one endpoint initiates each bidirectional edge.
                            if self.fid < peer and peer not in self.diallers:
                                self.diallers[peer] = asyncio.create_task(self.dial(peer))
                        LOG.info("%s neighbours: %s", self.fid, sorted(self.neighbours))
            except (OSError, ConnectionClosed, TimeoutError, ValueError, KeyError):
                LOG.warning("%s discovery unavailable; existing peer links remain active", self.fid)
            finally:
                self.discovery_online = False
            await asyncio.sleep(1)

    def hello(self, nonce):
        body = {"id": self.fid, "session": self.session, "nonce": nonce}
        return {"type": "hello", **body, "signature": self.private_key.sign(canonical(body)).hex()}

    async def peer_session(self, ws, expected=None):
        peer = None
        try:
            # Challenge-response proves possession of the discovered session key.
            nonce = uuid.uuid4().hex
            await send(ws, {"type": "challenge", "nonce": nonce})
            challenge = read(await asyncio.wait_for(ws.recv(), 5))
            if challenge.get("type") != "challenge":
                raise ValueError("Expected challenge")
            await send(ws, self.hello(challenge["nonce"]))
            hello = read(await asyncio.wait_for(ws.recv(), 5))
            peer = hello["id"]
            info = self.members.get(peer)
            if hello.get("type") != "hello" or hello["nonce"] != nonce or peer not in self.neighbours:
                raise ValueError("Unrecognised neighbour")
            if expected is not None and peer != expected:
                raise ValueError("Unexpected peer")
            if not info or hello["session"] != info["session"]:
                raise ValueError("Stale session")
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(info["key"])).verify(
                bytes.fromhex(hello["signature"]), canonical({k: hello[k] for k in ("id", "session", "nonce")}))
            if peer in self.peers:
                raise ValueError("Duplicate connection")
            self.peers[peer] = (ws, hello["session"])
            LOG.info("%s connected directly to %s", self.fid, peer)
            await send(ws, {"type": "summary", "ids": list(self.events)})
            async for raw in ws:
                msg = read(raw)
                if msg.get("type") == "summary":
                    # Use exact IDs, not just maximum vector-clock counters: out-of-order
                    # delivery can leave holes below the maximum sequence number.
                    known = set(msg["ids"])
                    for eid, event in list(self.events.items()):
                        if eid not in known:
                            await send(ws, {"type": "event", "event": event})
                elif msg.get("type") == "event":
                    self.ingest(msg["event"], via=peer)
                else:
                    raise ValueError("Unknown peer message")
        except (ValueError, KeyError, TypeError, InvalidSignature, TimeoutError):
            await ws.close(code=1008, reason="Invalid peer protocol")
        except ConnectionClosed:
            pass
        finally:
            if peer in self.peers and self.peers[peer][0] is ws:
                del self.peers[peer]

    async def set_isolated(self, isolated):
        self.isolated = isolated
        if isolated:
            for ws, _ in list(self.peers.values()):
                await ws.close()
            await self.emit("alert", {"fault": "network",
                                      "reason": "Cut off from neighbours and the AI; running in safe mode"})
            if self.edge:
                self.edge.enter_safe_mode()
            return {"ok": True, "isolated": True,
                    "reason": f"{self.fid} is cut off and running in safe mode: remote attacks can't reach it"}
        await self.emit("alert", {"fault": "network", "resolved": True, "reason": "Back in contact with neighbours and the AI"})
        return {"ok": True, "isolated": False, "reason": f"{self.fid} is reconnecting to its neighbours"}

    async def dial(self, peer):
        while peer in self.neighbours:
            if self.isolated:
                await asyncio.sleep(0.5)
                continue
            try:
                async with connect(self.members[peer]["endpoint"], proxy=None, **SOCKET_OPTIONS) as ws:
                    await self.peer_session(ws, peer)
            except (OSError, ConnectionClosed, TimeoutError):
                pass
            await asyncio.sleep(0.5)

    async def reconcile(self):
        while True:
            try:
                await asyncio.wait_for(self.changed.wait(), 1)
            except TimeoutError:
                pass
            self.changed.clear()
            summary = {"type": "summary", "ids": list(self.events)}
            for ws, _ in list(self.peers.values()):
                try:
                    await send(ws, summary)
                except (ConnectionClosed, TimeoutError):
                    await ws.close()
            await asyncio.sleep(0.05)

    async def handler(self, ws):
        if ws.request.path == "/peer":
            if self.isolated:
                await ws.close(code=1013, reason="Cut off from the network")
                return
            await self.peer_session(ws)
        elif ws.request.path == "/control":
            try:
                async for raw in ws:
                    try:
                        msg = read(raw)
                        if msg.get("channel") == "network":
                            # Requests over the store network (from the AI) are refused when cut off,
                            # and may only ask for the safe fixes the fridge allows remotely.
                            if self.isolated:
                                raise ValueError(f"{self.fid} is cut off from the network")
                            if msg.get("type") == "command" and msg.get("command") not in REMEDIATIONS:
                                raise ValueError("Only safe fixes can be requested over the network")
                        if msg.get("type") == "command" and msg.get("command") == "isolate":
                            await send(ws, {"type": "result", **await self.set_isolated(not self.isolated)})
                        elif msg.get("type") == "state":
                            await send(ws, {"type": "state", "state": self.snapshot(compact=bool(msg.get("compact")))})
                        elif msg.get("type") == "emit":
                            event = await self.emit(msg["kind"], msg.get("data", {}))
                            await send(ws, {"type": "emitted", "event": event})
                        elif (msg.get("type") == "command" and self.edge and self.isolated
                              and self.edge.starts_attack(msg.get("command"))):
                            # No network, no attack surface: the command never reaches the fridge.
                            await self.emit("alert", {"fault": "blocked", "command": msg["command"],
                                                      "reason": "Blocked a remote attack while cut off"})
                            await send(ws, {"type": "result", "ok": False, "blocked": True,
                                            "reason": f"Attack blocked: {self.fid} is cut off from the network and "
                                                      f"running in safe mode, so remote commands can't reach it"})
                        elif msg.get("type") == "command" and self.edge:
                            result = self.edge.command(msg["command"])
                            if result.get("rejected"):
                                await self.emit("alert", result)
                            await send(ws, {"type": "result", **result})
                        else:
                            raise ValueError("Expected state or emit")
                    except (ValueError, KeyError, TypeError) as exc:
                        await send(ws, {"type": "error", "message": str(exc)})
            except ConnectionClosed:
                pass
        else:
            await ws.close(code=1008, reason="Use /peer or /control")

    async def run(self, host, port):
        async with serve(self.handler, host, port, origins=[None], **SOCKET_OPTIONS):
            tasks = [asyncio.create_task(self.discover()), asyncio.create_task(self.reconcile())]
            if self.edge:
                tasks.append(asyncio.create_task(self.edge_loop()))
            try:
                await asyncio.Future()
            finally:
                for task in [*tasks, *self.diallers.values()]:
                    task.cancel()
                await asyncio.gather(*tasks, *self.diallers.values(), return_exceptions=True)
                for ws, _ in list(self.peers.values()):
                    await ws.close()

    async def edge_loop(self):
        while True:
            await self.edge.tick()
            await asyncio.sleep(0.9)


async def main(args):
    if args.command == "discovery":
        discovery = Discovery()
        async with serve(discovery.handler, args.host, args.port, origins=[None], **SOCKET_OPTIONS):
            LOG.info("Discovery listening on ws://%s:%s", args.host, args.port)
            await asyncio.Future()
    elif args.command == "node":
        if os.getenv("FRIDGE_EXIT_WITH_PARENT") == "1":
            exit_with_parent()
        endpoint = f"ws://{args.advertise_host or args.host}:{args.port}/peer"
        node = Fridge(args.id, endpoint, args.discovery)
        if args.simulate:
            from .edge import EdgeController
            node.edge = EdgeController(node, args.target, args.capacity, args.stock, args.frozen)
        await node.run(args.host, args.port)
    else:
        async with connect(args.url, proxy=None, **SOCKET_OPTIONS) as ws:
            msg = {"type": "state"} if args.command == "state" else {
                "type": "emit", "kind": args.kind, "data": json.loads(args.data)}
            await send(ws, msg)
            print(json.dumps(read(await ws.recv()), indent=2))


def exit_with_parent():
    """Exit when the server that started us stops, even if it crashed or its window was closed.

    The server keeps a pipe open to our stdin; when the server dies the pipe closes and the
    read returns. This stops old fridges (with old logs) joining the next server session.
    """
    import threading

    def watch():
        try:
            sys.stdin.read()
        finally:
            os._exit(0)

    threading.Thread(target=watch, daemon=True).start()


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, port in (("discovery", 9000), ("node", 9001)):
        sub = commands.add_parser(name)
        sub.add_argument("--host", default="127.0.0.1")
        sub.add_argument("--port", type=int, default=port)
        if name == "node":
            sub.add_argument("--id", required=True)
            sub.add_argument("--advertise-host")
            sub.add_argument("--discovery", default="ws://127.0.0.1:9000")
            sub.add_argument("--simulate", action="store_true")
            sub.add_argument("--target", type=float, default=3.0)
            sub.add_argument("--capacity", type=int, default=60)
            sub.add_argument("--stock", type=int, default=35)
            sub.add_argument("--frozen", action="store_true")
    for name in ("state", "emit"):
        sub = commands.add_parser(name)
        sub.add_argument("--url", default="ws://127.0.0.1:9001/control")
        if name == "emit":
            sub.add_argument("--kind", required=True, choices=["telemetry", "alert", "rescue_request", "capacity_offer"])
            sub.add_argument("--data", default="{}", help="JSON object")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main(parser.parse_args()))


if __name__ == "__main__":
    cli()