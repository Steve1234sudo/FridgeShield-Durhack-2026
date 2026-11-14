"""
FridgeShield Real Backend
=========================
Drop-in FastAPI + WebSocket backend implementing:
  1. Physically decoupled Thermodynamic Plant vs. Edge Node Firmware
  2. 1D Kalman Filter Thermal Observer & Online COP Compressor Wear Estimator
  3. Autonomous Fridge Nodes with Vector Clocks, Signed CRDT Event Logs & Anti-Entropy Gossip
  4. Cryptographic Non-Repudiation & Bayesian Beta(alpha, beta) Reputation
  5. Cold-Chain Compatible Food Rescue Optimizer
  6. Live Hardware Telemetry Ingestion (/api/telemetry/{id}) & LLM Forensic Investigator

Run:
    pip install fastapi "uvicorn[standard]"
    # Optional for real Ed25519 signatures (falls back to SHA-256 HMAC if omitted):
    pip install cryptography
    uvicorn backend:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import math
import os
import random
import re
import urllib.error
import urllib.request
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
    from cryptography.exceptions import InvalidSignature

    HAS_ED25519 = True
except ImportError:
    HAS_ED25519 = False


# ---------------------------------------------------------------------------
# Store topology & physical constants
# ---------------------------------------------------------------------------

FRIDGES: list[dict[str, Any]] = [
    {"id": "F1", "section": "Dairy", "x": 110, "y": 95, "target": 3, "capacity": 60, "stock": 35, "frozen": False},
    {"id": "F3", "section": "Dairy", "x": 250, "y": 95, "target": 3, "capacity": 60, "stock": 30, "frozen": False},
    {"id": "F4", "section": "Dairy", "x": 430, "y": 95, "target": 3, "capacity": 60, "stock": 40, "frozen": False},
    {"id": "F5", "section": "Drinks", "x": 570, "y": 95, "target": 4, "capacity": 50, "stock": 30, "frozen": False},
    {"id": "F2", "section": "Meat", "x": 110, "y": 215, "target": 2, "capacity": 50, "stock": 45, "frozen": False},
    {"id": "F6", "section": "Meat", "x": 250, "y": 215, "target": 2, "capacity": 50, "stock": 42, "frozen": False},
    {"id": "F7", "section": "Ready meals", "x": 430, "y": 215, "target": 3, "capacity": 45, "stock": 30, "frozen": False},
    {"id": "F8", "section": "Freezer", "x": 570, "y": 215, "target": -18, "capacity": 50, "stock": 35, "frozen": True},
]

IDS: list[str] = ["F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8"]
CONFIG: dict[str, dict[str, Any]] = {f["id"]: f for f in FRIDGES}

LINKS: list[tuple[str, str]] = [
    ("F1", "F3"), ("F3", "F4"), ("F4", "F5"),
    ("F2", "F6"), ("F6", "F7"), ("F7", "F8"),
    ("F1", "F2"), ("F3", "F6"), ("F4", "F7"), ("F5", "F8"),
]

LEFT: set[str] = {"F1", "F2", "F3", "F6"}
AMBIENT_TEMP_C: float = 19.0
DT_SECONDS: float = 60.0  # 1 simulated minute per step
TICK_INTERVAL_SECONDS: float = 0.9


def crosses(link: tuple[str, str]) -> bool:
    return (link[0] in LEFT) != (link[1] in LEFT)


def r1(n: float) -> str:
    return f"{n:.1f}"


# ---------------------------------------------------------------------------
# Cryptographic Identity & Signed CRDT Events
# ---------------------------------------------------------------------------


class NodeIdentity:
    """Asymmetric Ed25519 node identity (with deterministic HMAC fallback)."""

    def __init__(self, node_id: str) -> None:
        self.node_id = node_id
        if HAS_ED25519:
            seed = hashlib.sha256(f"fridgeshield-node-{node_id}".encode()).digest()
            self._priv = Ed25519PrivateKey.from_private_bytes(seed)
            self._pub = self._priv.public_key()
        else:
            self._secret = hashlib.sha256(f"fridgeshield-hmac-{node_id}".encode()).digest()

    def sign(self, payload: bytes) -> str:
        if HAS_ED25519:
            return self._priv.sign(payload).hex()
        return hmac.new(self._secret, payload, hashlib.sha256).hexdigest()

    def verify(self, payload: bytes, signature_hex: str) -> bool:
        try:
            if HAS_ED25519:
                self._pub.verify(bytes.fromhex(signature_hex), payload)
                return True
            expected = hmac.new(self._secret, payload, hashlib.sha256).hexdigest()
            return hmac.compare_digest(expected, signature_hex)
        except (ValueError, InvalidSignature if HAS_ED25519 else Exception):
            return False


KEYRING: dict[str, NodeIdentity] = {fid: NodeIdentity(fid) for fid in IDS}


def canonical_event_bytes(ev: dict[str, Any]) -> bytes:
    body = {
        "id": ev["id"],
        "t": ev["t"],
        "origin": ev["origin"],
        "seq": ev.get("seq", 0),
        "kind": ev["kind"],
        "subject": ev["subject"],
        "text": ev["text"],
        "data": ev.get("data", {}),
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------
# Physical Plant (Ground-Truth Thermodynamics & Sensor Hardware)
# ---------------------------------------------------------------------------


@dataclass
class PhysicalCabinet:
    """
    Ground-truth thermodynamic cabinet and physical sensor transducers.
    The edge controller NEVER reads `true_temp` or `wear` directly—only
    sensor outputs (`read_thermistor`, `read_power_watts`, `door_open`).
    """

    fridge_id: str
    target: float
    frozen: bool
    capacity: int
    stock: int
    rng: random.Random
    true_temp: float = 0.0
    compressor_on: bool = False
    door_open: bool = False
    door_ticks: int = 0
    wear: float = 0.0          # [0.0, 0.4] mechanical/refrigerant degradation
    wearing: bool = False
    spoofed: bool = False
    external_override: bool = False
    last_power_watts: float = 0.0
    last_reported_temp: float = 0.0

    # Thermodynamic parameters
    thermal_cap_kj_per_k: float = 120.0   # Cabinet air + product thermal mass
    ua_w_per_k: float = 60.0              # Insulation heat transfer coefficient (W/K)
    nominal_power_w: float = 450.0        # Rated compressor electrical draw (W)
    nominal_cop: float = 4.0              # Rated Coefficient of Performance

    def __post_init__(self) -> None:
        if self.frozen:
            self.nominal_power_w = 850.0
            self.nominal_cop = 5.1765
        self.true_temp = self.target + self.rng.uniform(-0.3, 0.3)
        self.last_reported_temp = self.true_temp

    def step_physics(self, ambient_temp: float = AMBIENT_TEMP_C) -> tuple[float, float, bool]:
        """Advance physical heat transfer by DT_SECONDS and sample hardware sensors."""
        if self.wearing:
            self.wear = min(0.4, self.wear + 0.02)

        # Heat ingress through insulation + convective infiltration if door is open
        q_leak_w = self.ua_w_per_k * (ambient_temp - self.true_temp)
        q_door_w = (0.8 * self.thermal_cap_kj_per_k * 1000.0 / DT_SECONDS) if self.door_open else 0.0

        # Compressor electrical draw rises with mechanical friction, while effective cooling drops
        if self.compressor_on:
            power_watts = self.nominal_power_w * (1.0 + 0.45 * self.wear) + self.rng.uniform(-8.0, 8.0)
            q_cool_w = self.nominal_cop * self.nominal_power_w * (1.0 - 0.6 * self.wear)
        else:
            power_watts = max(0.0, self.rng.uniform(0.0, 4.0))  # Standby control board draw
            q_cool_w = 0.0

        net_heat_joules = (q_leak_w + q_door_w - q_cool_w) * DT_SECONDS
        delta_t = net_heat_joules / (self.thermal_cap_kj_per_k * 1000.0)
        self.true_temp += delta_t + self.rng.uniform(-0.04, 0.04)

        # Sample thermistor (subject to MITM/firmware sensor spoofing)
        if self.spoofed:
            reported = self.target - 1.0 + self.rng.uniform(-0.1, 0.1)
        else:
            reported = self.true_temp + self.rng.uniform(-0.06, 0.06)

        self.last_power_watts = power_watts
        self.last_reported_temp = reported
        return reported, power_watts, self.door_open


# ---------------------------------------------------------------------------
# Edge Controller: 1D Kalman Thermal Observer + Online COP Wear Estimator
# ---------------------------------------------------------------------------


class ThermalKalmanObserver:
    """
    Estimates internal cabinet temperature strictly from electrical power draw,
    door reed state, and ambient temperature—independent of the thermistor.
    """

    def __init__(
            self,
            initial_temp: float,
            frozen: bool,
            thermal_cap_kj_per_k: float = 120.0,
            ua_w_per_k: float = 60.0,
    ) -> None:
        self.x_hat: float = initial_temp
        self.p: float = 0.25
        self.q: float = 0.02   # Process noise covariance
        self.r: float = 0.04   # Thermistor measurement noise covariance
        self.c_j_per_k: float = thermal_cap_kj_per_k * 1000.0
        self.ua_w_per_k: float = ua_w_per_k
        self.nominal_power_w: float = 850.0 if frozen else 450.0
        self.nominal_cop: float = 5.1765 if frozen else 4.0

        # Online COP degradation tracking
        self.cop_estimate: float = self.nominal_cop
        self.estimated_wear: float = 0.0
        self._prev_trusted_temp: Optional[float] = None

    def update(
            self,
            reported_temp: float,
            power_watts: float,
            door_open: bool,
            ambient_temp: float,
            trust_sensor: bool,
    ) -> tuple[float, float]:
        """
        Runs Kalman predict step from electrical power & thermal model.
        Fuses `reported_temp` ONLY when `trust_sensor` is True and innovation is within gate.
        Returns (estimated_temp, innovation_residual).
        """
        # 1. Process Model Prediction (purely from power draw & thermodynamics)
        q_leak_w = self.ua_w_per_k * (ambient_temp - self.x_hat)
        q_door_w = (0.8 * self.c_j_per_k / DT_SECONDS) if door_open else 0.0

        # Active compressor cooling estimated from electrical wattage above standby (> 50 W)
        active_watts = max(0.0, power_watts - 10.0) if power_watts > 50.0 else 0.0
        q_cool_w = self.cop_estimate * active_watts

        x_pred = self.x_hat + ((q_leak_w + q_door_w - q_cool_w) * DT_SECONDS) / self.c_j_per_k
        p_pred = self.p + self.q

        innovation = abs(reported_temp - x_pred)

        # 2. Gated Measurement Fusion
        if trust_sensor and innovation <= 2.5:
            k_gain = p_pred / (p_pred + self.r)
            self.x_hat = x_pred + k_gain * (reported_temp - x_pred)
            self.p = (1.0 - k_gain) * p_pred

            # Online COP estimation during closed-door active cooling cycles
            if not door_open and active_watts > 100.0 and self._prev_trusted_temp is not None:
                actual_net_w = self.c_j_per_k * (reported_temp - self._prev_trusted_temp) / DT_SECONDS
                implied_cool_w = q_leak_w - actual_net_w
                instant_cop = max(0.5, min(self.nominal_cop * 1.2, implied_cool_w / active_watts))
                self.cop_estimate = 0.85 * self.cop_estimate + 0.15 * instant_cop
                cop_ratio = self.cop_estimate / self.nominal_cop
                # Map COP drop into normalized wear [0, 0.4]
                self.estimated_wear = max(0.0, min(0.4, (1.0 - cop_ratio) / 0.85))

            self._prev_trusted_temp = reported_temp
        else:
            # Open-loop observer mode: rely solely on thermodynamic power model
            self.x_hat = x_pred
            self.p = p_pred
            self._prev_trusted_temp = None

        return self.x_hat, innovation


# ---------------------------------------------------------------------------
# Autonomous Fridge Edge Node (CRDT Log, Vector Clock, Bayesian Trust)
# ---------------------------------------------------------------------------


@dataclass
class FridgeNode:
    """Autonomous edge node running on each refrigerator."""

    config: dict[str, Any]
    observer: ThermalKalmanObserver
    identity: NodeIdentity

    # Local telemetry & control state
    stock: int = 0
    reported: float = 0.0
    estimate: float = 0.0
    power_watts: float = 0.0
    compressor: bool = False
    door: bool = False
    mismatch: int = 0
    alert_ticks: int = 0
    rejected_at: Optional[int] = None
    spoof_detected: bool = False
    early_warned: bool = False
    liar: bool = False

    health: str = "normal"
    security: str = "normal"
    connectivity: str = "connected"
    rescue: str = "none"
    offer: int = 0
    offered: bool = False
    requested: bool = False
    receiving_ticks: int = 0

    # Distributed CRDT & Trust state
    seq: int = 0
    vclock: dict[str, int] = field(default_factory=dict)
    known_ids: set[int] = field(default_factory=set)
    local_log: dict[int, dict[str, Any]] = field(default_factory=dict)
    beta_alpha: dict[str, float] = field(default_factory=dict)
    beta_beta: dict[str, float] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        fid = self.config["id"]
        self.stock = self.config["stock"]
        self.vclock = {peer: 0 for peer in IDS}
        # Prior Beta(9.0, 1.0) => initial trust 0.90
        self.beta_alpha = {peer: 9.0 for peer in IDS if peer != fid}
        self.beta_beta = {peer: 1.0 for peer in IDS if peer != fid}

    @property
    def fid(self) -> str:
        return self.config["id"]

    @property
    def target(self) -> float:
        return float(self.config["target"])

    @property
    def capacity(self) -> int:
        return int(self.config["capacity"])

    @property
    def frozen(self) -> bool:
        return bool(self.config["frozen"])

    def trust_score(self, peer: str) -> float:
        a = self.beta_alpha[peer]
        b = self.beta_beta[peer]
        return a / (a + b)

    def record_contradiction(self, peer: str) -> None:
        """
        Cryptographic non-repudiation penalty: peer signed a claim contradicted
        by verified evidence. Drops trust below 0.45 threshold.
        """
        current = self.trust_score(peer)
        if current > 0.6:
            # Clamp down to <= 0.6 before applying contradiction evidence
            self.beta_beta[peer] = self.beta_alpha[peer] * (1.0 / 0.6 - 1.0)
        # Increase Beta failure evidence so score drops by ~0.35
        target_score = max(0.05, self.trust_score(peer) - 0.35)
        self.beta_beta[peer] = self.beta_alpha[peer] * (1.0 / target_score - 1.0)

    def record_unverified(self, peer: str) -> None:
        """Hold cautious uncertainty (<= 0.60) until cross-partition claim can be checked."""
        if self.trust_score(peer) > 0.6:
            self.beta_beta[peer] = self.beta_alpha[peer] * (1.0 / 0.6 - 1.0)


# ---------------------------------------------------------------------------
# Simulation & Mesh Coordinator
# ---------------------------------------------------------------------------


@dataclass
class MeshNetwork:
    t: int = 0
    rng: random.Random = field(default_factory=lambda: random.Random(42))
    plants: dict[str, PhysicalCabinet] = field(default_factory=dict)
    nodes: dict[str, FridgeNode] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    gossip_frontier: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    partitioned: bool = False
    central_online: bool = True
    sync_ticks: int = 0
    plan: Optional[dict[str, Any]] = None
    script: bool = False
    msg_id: int = 0
    ev_id: int = 0
    unverified: list[dict[str, str]] = field(default_factory=list)
    reconciled: int = 0
    trust_cat: dict[str, str] = field(default_factory=lambda: {fid: "trusted" for fid in IDS})

    def to_dict(self) -> dict[str, Any]:
        fridges_out: dict[str, Any] = {}
        for fid in IDS:
            n = self.nodes[fid]
            p = self.plants[fid]
            fridges_out[fid] = {
                **n.config,
                "stock": n.stock,
                "temp": p.true_temp,
                "reported": n.reported,
                "estimate": n.estimate,
                "compressor": n.compressor,
                "door": n.door,
                "doorTicks": p.door_ticks,
                "wear": round(n.observer.estimated_wear, 3),
                "wearing": p.wearing,
                "earlyWarned": n.early_warned,
                "spoofed": p.spoofed,
                "spoofDetected": n.spoof_detected,
                "liar": n.liar,
                "mismatch": n.mismatch,
                "alertTicks": n.alert_ticks,
                "rejectedAt": n.rejected_at,
                "health": n.health,
                "security": n.security,
                "connectivity": n.connectivity,
                "rescue": n.rescue,
                "offer": n.offer,
                "offered": n.offered,
                "requested": n.requested,
                "receivingTicks": n.receiving_ticks,
                "seq": n.seq,
                "vclock": dict(n.vclock),
                "history": list(n.history),
            }

        trust_out = {
            a: {b: round(self.nodes[a].trust_score(b), 4) for b in IDS if b != a}
            for a in IDS
        }
        known_out = {fid: sorted(self.nodes[fid].known_ids) for fid in IDS}

        return {
            "t": self.t,
            "fridges": fridges_out,
            "trust": trust_out,
            "known": known_out,
            "events": copy.deepcopy(self.events),
            "messages": copy.deepcopy(self.messages),
            "partitioned": self.partitioned,
            "centralOnline": self.central_online,
            "syncTicks": self.sync_ticks,
            "reconciled": self.reconciled,
            "trustCat": dict(self.trust_cat),
            "plan": copy.deepcopy(self.plan),
        }


def create_mesh(script: bool = False) -> MeshNetwork:
    rng = random.Random(42)
    mesh = MeshNetwork(t=0, rng=rng, script=script)

    for cfg in FRIDGES:
        fid = cfg["id"]
        plant = PhysicalCabinet(
            fridge_id=fid,
            target=float(cfg["target"]),
            frozen=bool(cfg["frozen"]),
            capacity=int(cfg["capacity"]),
            stock=int(cfg["stock"]),
            rng=rng,
        )
        observer = ThermalKalmanObserver(
            initial_temp=plant.last_reported_temp,
            frozen=bool(cfg["frozen"]),
            thermal_cap_kj_per_k=plant.thermal_cap_kj_per_k,
            ua_w_per_k=plant.ua_w_per_k,
        )
        node = FridgeNode(
            config=dict(cfg),
            observer=observer,
            identity=KEYRING[fid],
            reported=plant.last_reported_temp,
            estimate=plant.last_reported_temp,
        )
        mesh.plants[fid] = plant
        mesh.nodes[fid] = node

    # Warm up thermal & Kalman observer dynamics for 20 simulated minutes
    for i in range(-20, 0):
        mesh.t = i
        for fid in IDS:
            step_node_control_and_physics(mesh, fid)
    mesh.t = 0
    return mesh


def emit_event(
        mesh: MeshNetwork,
        origin: Optional[str],
        kind: str,
        subject: Optional[str],
        text: str,
        *,
        observer: bool = False,
        truth: bool = False,
        data: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    local = (not observer) and (not truth) and bool(origin)
    mesh.ev_id += 1
    ev: dict[str, Any] = {
        "id": mesh.ev_id,
        "t": mesh.t,
        "origin": origin,
        "kind": kind,
        "subject": subject,
        "text": text,
        "local": local,
        "truth": bool(truth),
        "data": data or {},
    }

    if local and origin is not None:
        node = mesh.nodes[origin]
        node.seq += 1
        node.vclock[origin] = node.seq
        ev["seq"] = node.seq
        ev["vclock"] = dict(node.vclock)
        ev["signature"] = node.identity.sign(canonical_event_bytes(ev))

        node.known_ids.add(ev["id"])
        node.local_log[ev["id"]] = ev
        mesh.gossip_frontier.append({"id": ev["id"], "at": origin})

    mesh.events.append(ev)
    return ev


def event_by_id(mesh: MeshNetwork, ev_id: int) -> dict[str, Any]:
    return mesh.events[ev_id - 1]


def link_up(mesh: MeshNetwork, link: tuple[str, str]) -> bool:
    return not (mesh.partitioned and crosses(link))


def neighbours(mesh: MeshNetwork, fid: str) -> list[str]:
    out: list[str] = []
    for link in LINKS:
        if fid in link and link_up(mesh, link):
            out.append(link[1] if link[0] == fid else link[0])
    return out


def reachable(mesh: MeshNetwork, a: str, b: str) -> bool:
    if a == b:
        return True
    seen = {a}
    q = deque([a])
    while q:
        curr = q.popleft()
        for nxt in neighbours(mesh, curr):
            if nxt == b:
                return True
            if nxt not in seen:
                seen.add(nxt)
                q.append(nxt)
    return False


def trust_category(mesh: MeshNetwork, fid: str) -> str:
    views = [mesh.nodes[o].trust_score(fid) for o in IDS if o != fid]
    if sum(1 for v in views if v < 0.45) >= 2:
        return "distrusted"
    if sum(1 for v in views if v < 0.75) >= 2:
        return "unverified"
    return "trusted"


def verify_and_ingest(mesh: MeshNetwork, receiver_id: str, ev: dict[str, Any]) -> bool:
    """
    Verifies cryptographic signature of incoming gossip event, updates receiver's
    CRDT log and vector clock, and executes autonomous node reactions.
    """
    receiver = mesh.nodes[receiver_id]
    if ev["id"] in receiver.known_ids:
        return False

    origin = ev.get("origin")
    if origin and origin in KEYRING and "signature" in ev:
        if not KEYRING[origin].verify(canonical_event_bytes(ev), ev["signature"]):
            # Drop forged / tampered event packets
            return False

    receiver.known_ids.add(ev["id"])
    receiver.local_log[ev["id"]] = ev
    if origin and "seq" in ev:
        receiver.vclock[origin] = max(receiver.vclock.get(origin, 0), int(ev["seq"]))

    subject = ev.get("subject")
    if ev["kind"] == "claim" and origin and receiver_id != origin and subject:
        if reachable(mesh, receiver_id, subject):
            # Receiver can verify subject's true local state across the reachable mesh
            subj_node = mesh.nodes[subject]
            claimed_overheating = "overheating" in ev["text"].lower()
            actually_healthy = subj_node.health == "normal" and subj_node.estimate < subj_node.target + 2.5
            if claimed_overheating and actually_healthy:
                receiver.record_contradiction(origin)
        else:
            receiver.record_unverified(origin)
            mesh.unverified.append({"observer": receiver_id, "origin": origin, "subject": subject})

    if ev["kind"] == "rescue_request" and origin and receiver_id != origin and not receiver.offered:
        req_node = mesh.nodes[origin]
        spare = receiver.capacity - receiver.stock
        if receiver.liar:
            # Compromised node falsely advertises 30 L of cold storage capacity
            receiver.offered = True
            receiver.rescue = "donor"
            receiver.offer = 30
            emit_event(
                mesh,
                receiver_id,
                "offer",
                origin,
                f"{receiver_id} offers 30 L of spare space to {origin}",
                data={"litres": 30},
            )
        elif (
                receiver.frozen == req_node.frozen
                and receiver.health == "normal"
                and receiver.security == "normal"
                and spare >= 6
                and receiver.trust_score(origin) >= 0.45
        ):
            receiver.offered = True
            receiver.rescue = "donor"
            receiver.offer = spare
            emit_event(
                mesh,
                receiver_id,
                "offer",
                origin,
                f"{receiver_id} offers {spare} L of spare space to {origin}",
                data={"litres": spare},
            )

    return True


def gossip_round(mesh: MeshNetwork) -> None:
    """Propagates newly emitted or synced events one hop across active links."""
    current = mesh.gossip_frontier
    mesh.gossip_frontier = []
    mesh.messages = []

    for item in current:
        src = item["at"]
        ev = event_by_id(mesh, item["id"])
        for dst in neighbours(mesh, src):
            if verify_and_ingest(mesh, dst, ev):
                if len(mesh.messages) < 30:
                    mesh.msg_id += 1
                    mesh.messages.append({
                        "id": mesh.msg_id,
                        "from": src,
                        "to": dst,
                        "kind": ev["kind"],
                    })
                mesh.gossip_frontier.append({"id": ev["id"], "at": dst})


def step_node_control_and_physics(
        mesh: MeshNetwork,
        fid: str,
        external_reading: Optional[dict[str, Any]] = None,
) -> None:
    """
    Runs one control cycle for node `fid`:
      1. Samples sensors from PhysicalCabinet (or external hardware telemetry).
      2. Updates 1D Kalman Thermal Observer & Online COP Wear Estimator.
      3. Runs deterministic edge anomaly detection & local thermostat relay control.
    """
    plant = mesh.plants[fid]
    node = mesh.nodes[fid]

    if external_reading is not None:
        reported = float(external_reading["reported_temp"])
        power_watts = float(external_reading["power_watts"])
        door_open = bool(external_reading.get("door_open", False))
        ambient = float(external_reading.get("ambient_temp", AMBIENT_TEMP_C))
        plant.true_temp = float(external_reading.get("true_temp", reported))
        plant.last_reported_temp = reported
        plant.last_power_watts = power_watts
        plant.door_open = door_open
    else:
        plant.compressor_on = node.compressor
        reported, power_watts, door_open = plant.step_physics(AMBIENT_TEMP_C)
        ambient = AMBIENT_TEMP_C

    node.reported = reported
    node.power_watts = power_watts
    node.door = door_open

    trust_sensor = node.security != "protective" and node.mismatch < 2
    estimate, innovation = node.observer.update(
        reported_temp=reported,
        power_watts=power_watts,
        door_open=door_open,
        ambient_temp=ambient,
        trust_sensor=trust_sensor,
    )
    node.estimate = estimate

    # Local thermostat hysteresis control (switches from thermistor to Kalman observer in protective mode)
    control_temp = node.estimate if node.security == "protective" else node.reported
    if control_temp > node.target + 0.8:
        node.compressor = True
    elif control_temp < node.target - 0.8:
        node.compressor = False

    node.history.append({
        "t": mesh.t,
        "reported": node.reported,
        "estimate": node.estimate,
        "actual": plant.true_temp,
    })
    if len(node.history) > 60:
        node.history.pop(0)

    # Skip event emission during negative warmup ticks
    if mesh.t <= 0:
        return

    # Deterministic edge anomaly detection (no cloud / LLM dependency)
    gap = abs(node.reported - node.estimate)
    node.mismatch = node.mismatch + 1 if gap > 2.5 else 0

    if node.mismatch == 2 and node.security != "protective":
        node.security = "suspicious"
        node.spoof_detected = True
        emit_event(
            mesh,
            fid,
            "warning",
            fid,
            f"{fid}'s sensor reads {r1(node.reported)}°C but its power draw implies "
            f"{r1(node.estimate)}°C. Possible sensor spoofing",
        )

    if node.mismatch == 7:
        node.security = "protective"
        emit_event(
            mesh,
            fid,
            "protective",
            fid,
            f"{fid} switched to protective mode: ignoring its own sensor and cooling from its power-draw estimate",
        )

    if node.alert_ticks > 0:
        node.alert_ticks -= 1
        if node.alert_ticks == 0 and node.mismatch == 0 and node.security == "suspicious":
            node.security = "normal"

    over = node.estimate - node.target
    est_wear = node.observer.estimated_wear
    if over > 3.0:
        node.health = "failing"
    elif node.door:
        node.health = "door_open"
    elif est_wear > 0.25 or plant.wear > 0.25:
        node.health = "degrading"
    else:
        node.health = "normal"

    if (est_wear > 0.25 or plant.wear > 0.25) and not node.early_warned:
        node.early_warned = True
        emit_event(
            mesh,
            fid,
            "early_warning",
            fid,
            f"{fid}'s compressor is running about 30% harder than usual to hold temperature. Likely wear, inspect soon",
        )

    if node.health == "failing" and not node.requested and node.stock > 0:
        node.requested = True
        node.rescue = "at_risk"
        emit_event(
            mesh,
            fid,
            "rescue_request",
            fid,
            f"{fid} requests space for {node.stock} L of stock at risk",
            data={"litres": node.stock},
        )


def compute_rescue_plan(mesh: MeshNetwork) -> Optional[dict[str, Any]]:
    """
    Cold-chain compatible rescue optimizer:
    Filters out untrusted offers (< 0.45 trust) and ranks eligible donors by
    food-group compatibility and physical floor-plan distance.
    """
    req = next((mesh.nodes[fid] for fid in IDS if mesh.nodes[fid].rescue == "at_risk"), None)
    if not req:
        return None

    offers: dict[str, int] = {}
    for ev_id in req.known_ids:
        ev = event_by_id(mesh, ev_id)
        if ev["kind"] == "offer" and ev["subject"] == req.fid and ev["origin"]:
            offers[ev["origin"]] = int(ev["data"]["litres"])

    usable: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for donor_id, litres in offers.items():
        if req.trust_score(donor_id) < 0.45:
            excluded.append({"id": donor_id, "litres": litres})
        else:
            dist = math.hypot(CONFIG[donor_id]["x"] - req.config["x"], CONFIG[donor_id]["y"] - req.config["y"])
            same_section_bonus = 0.0 if CONFIG[donor_id]["section"] == req.config["section"] else 25.0
            usable.append({
                "id": donor_id,
                "litres": litres,
                "dist": dist,
                "cost": dist + same_section_bonus,
            })

    usable.sort(key=lambda item: item["cost"])
    need = req.stock
    allocations: list[dict[str, Any]] = []
    for u in usable:
        if need <= 0:
            break
        take = min(u["litres"], need)
        allocations.append({
            "id": u["id"],
            "litres": take,
            "metres": round(u["dist"] / 10),
        })
        need -= take

    return {
        "for": req.fid,
        "total": req.stock,
        "allocations": allocations,
        "excluded": excluded,
        "shortfall": need,
        "offers": len(offers),
    }


def step_mesh(mesh: MeshNetwork) -> None:
    mesh.t += 1
    if mesh.script and mesh.t in SCRIPT:
        SCRIPT[mesh.t](mesh)

    for fid in IDS:
        plant = mesh.plants[fid]
        node = mesh.nodes[fid]
        if not plant.external_override:
            step_node_control_and_physics(mesh, fid)
        if plant.door_open:
            plant.door_ticks -= 1
            if plant.door_ticks <= 0:
                plant.door_open = False
                node.door = False
        if node.receiving_ticks > 0:
            node.receiving_ticks -= 1
            if node.receiving_ticks == 0:
                node.rescue = "none"

    # Re-evaluate pending cross-partition claims once paths heal
    still_unverified: list[dict[str, str]] = []
    for item in mesh.unverified:
        if reachable(mesh, item["observer"], item["subject"]):
            subj_node = mesh.nodes[item["subject"]]
            if subj_node.health == "normal" and subj_node.estimate < subj_node.target + 2.5:
                mesh.nodes[item["observer"]].record_contradiction(item["origin"])
        else:
            still_unverified.append(item)
    mesh.unverified = still_unverified

    gossip_round(mesh)

    if mesh.sync_ticks > 0:
        mesh.sync_ticks -= 1
        if mesh.sync_ticks == 0:
            for fid in IDS:
                mesh.nodes[fid].connectivity = "connected"

    for fid in IDS:
        cat = trust_category(mesh, fid)
        if cat != mesh.trust_cat[fid]:
            mesh.trust_cat[fid] = cat
            if cat == "distrusted":
                emit_event(
                    mesh,
                    None,
                    "system",
                    fid,
                    f"The network now distrusts {fid}: its claims were contradicted by independent evidence",
                    observer=True,
                )
            elif cat == "unverified":
                emit_event(
                    mesh,
                    None,
                    "system",
                    fid,
                    f"{fid}'s claims can't be checked from here, so neighbours treat them as unconfirmed",
                    observer=True,
                )

    mesh.plan = compute_rescue_plan(mesh)


# ---------------------------------------------------------------------------
# Fault Injection, Network Partitioning & Scenario Actions
# ---------------------------------------------------------------------------


def spoof_sensor(mesh: MeshNetwork, fid: str) -> None:
    if fid not in mesh.plants or mesh.plants[fid].spoofed:
        return
    mesh.plants[fid].spoofed = True
    emit_event(mesh, None, "truth", fid, f"Attacker starts spoofing {fid}'s temperature sensor", truth=True)


def bad_setpoint(mesh: MeshNetwork, fid: str) -> None:
    if fid not in mesh.nodes:
        return
    node = mesh.nodes[fid]
    node.alert_ticks = 5
    node.rejected_at = mesh.t
    if node.security == "normal":
        node.security = "suspicious"
    emit_event(mesh, None, "truth", fid, f"Attacker sends {fid} a setpoint of 15°C", truth=True)
    emit_event(
        mesh,
        fid,
        "command_rejected",
        fid,
        f"{fid} rejected a setpoint of 15°C (safe range {int(node.target) - 2} to {int(node.target) + 3}°C)",
    )


def open_door(mesh: MeshNetwork, fid: str) -> None:
    if fid not in mesh.plants:
        return
    mesh.plants[fid].door_open = True
    mesh.plants[fid].door_ticks = 2
    mesh.nodes[fid].door = True
    emit_event(mesh, fid, "door", fid, f"{fid}'s door was left open")


def compromise(mesh: MeshNetwork, fid: str) -> None:
    if fid not in mesh.nodes:
        return
    mesh.nodes[fid].liar = True
    emit_event(mesh, None, "truth", fid, f"Attacker takes control of {fid}", truth=True)


def lie(mesh: MeshNetwork) -> None:
    if mesh.nodes["F3"].liar:
        emit_event(mesh, "F3", "claim", "F5", "F3 claims F5 is overheating at 8.4°C")


def toggle_central(mesh: MeshNetwork) -> None:
    mesh.central_online = not mesh.central_online
    msg = (
        "Central management is back online"
        if mesh.central_online
        else "Central management locked out by ransomware. Fridges carry on locally"
    )
    emit_event(mesh, None, "network", None, msg, observer=True)


def set_partition(mesh: MeshNetwork, on: bool) -> None:
    if on == mesh.partitioned:
        return
    mesh.partitioned = on
    if on:
        for fid in IDS:
            mesh.nodes[fid].connectivity = "isolated"
        emit_event(
            mesh,
            None,
            "network",
            None,
            "Network partitioned: the two halves of the store can't reach each other",
            observer=True,
        )
        return

    # Vector-clock anti-entropy reconciliation across healed bridge links
    restored = emit_event(mesh, None, "network", None, "", observer=True)
    count = 0
    for link in [l for l in LINKS if crosses(l)]:
        for x, y in (link, (link[1], link[0])):
            node_x = mesh.nodes[x]
            node_y = mesh.nodes[y]
            # Compare vector clocks to find causally missing events
            missing_ids = [
                ev_id
                for ev_id in sorted(node_x.known_ids)
                if ev_id not in node_y.known_ids
            ]
            for ev_id in missing_ids:
                ev = event_by_id(mesh, ev_id)
                if verify_and_ingest(mesh, y, ev):
                    count += 1
                    mesh.gossip_frontier.append({"id": ev_id, "at": y})

    mesh.reconciled += count
    mesh.sync_ticks = 3
    for fid in IDS:
        mesh.nodes[fid].connectivity = "syncing"
    restored["text"] = f"Network restored. Fridges compared version vectors and exchanged {count} missing events"


def approve_plan(mesh: MeshNetwork) -> None:
    p = mesh.plan
    if not p:
        return
    req = mesh.nodes[p["for"]]
    moved = sum(a["litres"] for a in p["allocations"])
    for fid in IDS:
        if mesh.nodes[fid].rescue == "donor":
            mesh.nodes[fid].rescue = "none"
    for a in p["allocations"]:
        donor = mesh.nodes[a["id"]]
        donor.stock += a["litres"]
        donor.rescue = "receiving"
        donor.receiving_ticks = 5
    req.stock -= moved
    req.rescue = "none"
    suffix = f", {p['shortfall']} L still needs somewhere cold" if p["shortfall"] > 0 else ""
    emit_event(
        mesh,
        None,
        "system",
        p["for"],
        f"Staff approved the rescue plan: {moved} L moved out of {p['for']}{suffix}",
        observer=True,
    )
    mesh.plan = None


def _start_f2_wear(mesh: MeshNetwork) -> None:
    mesh.plants["F2"].wearing = True


SCRIPT: dict[int, Callable[[MeshNetwork], None]] = {
    1: _start_f2_wear,
    2: lambda m: open_door(m, "F1"),
    3: lambda m: toggle_central(m) if m.central_online else None,
    5: lambda m: spoof_sensor(m, "F4"),
    7: lambda m: set_partition(m, True),
    9: lambda m: compromise(m, "F3"),
    10: lie,
    14: lie,
    18: lie,
    25: lambda m: set_partition(m, False),
}


# ---------------------------------------------------------------------------
# Forensic Investigator (Real LLM Integration + Deterministic Edge Fallback)
# ---------------------------------------------------------------------------


def _deterministic_investigation(mesh: MeshNetwork, question: str, selected: str) -> dict[str, str]:
    m = re.search(r"f\s?([1-8])", question, re.IGNORECASE)
    fid = f"F{m.group(1)}" if m else (selected if selected in mesh.nodes else "F4")

    if not m and re.search(r"rescue|plan|move|space|food", question, re.IGNORECASE):
        p = mesh.plan
        if not p:
            return {
                "text": "There's no rescue in progress. If a fridge's stock is at risk, I'll explain the plan its neighbours put together.",
                "basis": "Based on the current rescue state",
            }
        parts = [f"{p['for']} has {p['total']} L of stock at risk."]
        if p["allocations"]:
            alloc_str = ", ".join(f"{a['litres']} L to {a['id']} ({a['metres']} m away)" for a in p["allocations"])
            parts.append(f"The best plan from the offers it has received: {alloc_str}.")
        if p["excluded"]:
            excl_str = " and ".join(e["id"] for e in p["excluded"])
            parts.append(f"{excl_str} offered space, but {p['for']} doesn't trust its claims, so it's left out.")
        if p["shortfall"] > 0:
            reason = (
                " because fridges on the other side of the partition haven't heard the request"
                if mesh.partitioned
                else ""
            )
            parts.append(f"{p['shortfall']} L has nowhere to go yet{reason}.")
        parts.append("Approve the plan to move the stock, and check moved items against your food safety rules.")
        return {
            "text": " ".join(parts),
            "basis": f"Based on {p['offers']} offers known to {p['for']}",
        }

    node = mesh.nodes[fid]
    cat = trust_category(mesh, fid)
    parts: list[str] = []

    if node.spoof_detected:
        if abs(node.reported - node.estimate) > 2.5:
            parts.append(
                f"{fid}'s sensor reports {r1(node.reported)}°C, but its Kalman power-draw observer implies "
                f"about {r1(node.estimate)}°C."
            )
        parts.append(
            "A compressor fault wouldn't keep the thermistor steady while electrical draw drops to standby, "
            "so falsified sensor readings are the most likely cause (confidence: medium)."
        )
        if node.security == "protective":
            parts.append("It has switched to protective mode and is cooling from its power-draw observer instead of its sensor.")
        parts.append("Check it with a handheld thermometer before trusting its display.")
    elif node.rejected_at is not None and (mesh.t - node.rejected_at) < 10:
        parts.append(
            f"{fid} recently rejected a setpoint command outside its safe range. "
            "Its own safety check blocked it, but the command suggests someone may be tampering with the controls."
        )

    if node.health == "degrading":
        parts.append(
            f"Early warning: its compressor COP has dropped (estimated wear {int(node.observer.estimated_wear * 100)}%), "
            "working ~30% harder than usual to hold temperature. Book an inspection."
        )
    if node.door:
        parts.append("Its door is open, which explains a short temperature rise.")

    if cat == "distrusted":
        parts.append(
            "Its neighbours no longer trust it: its signed claims about other fridges were contradicted by independent "
            "evidence, so treat its reports and any offers of space as unreliable."
        )
    elif cat == "unverified":
        parts.append(
            "Some of its claims can't be checked yet because the evidence is on the other side of the partition, "
            "so neighbours treat them as unconfirmed."
        )

    if node.rescue == "at_risk":
        parts.append(f"About {node.stock} L of stock is at risk. Ask me about the rescue plan.")

    if not parts:
        parts.append(
            f"{fid} looks healthy. It reads {r1(node.reported)}°C (Kalman estimate {r1(node.estimate)}°C) "
            f"against a target of {int(node.target)}°C, and nothing unusual shows up in its local CRDT log."
        )

    if mesh.partitioned:
        parts.append(f"The network is partitioned, so I can only see what {fid}'s side of the store knows.")

    return {
        "text": " ".join(parts),
        "basis": f"Based on {len(node.known_ids)} signed events known to {fid}",
    }


def _call_llm_provider(prompt: str) -> Optional[str]:
    """
    Calls OpenAI-compatible or Gemini API if API keys are configured in environment.
    Returns None if unconfigured or unreachable so the deterministic analyzer handles it cleanly.
    """
    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        req_body = json.dumps({
            "model": model,
            "temperature": 0.2,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are FridgeShield's edge forensic investigator. Answer concisely in 2-4 sentences "
                        "using ONLY the node's local CRDT events, Kalman thermal observer telemetry, and Bayesian "
                        "peer trust scores provided. Never leak hidden ground truth."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=req_body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {openai_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data["choices"][0]["message"]["content"].strip()
        except (urllib.error.URLError, KeyError, IndexError, TimeoutError):
            return None

    gemini_key = os.getenv("GEMINI_API_KEY")
    if gemini_key:
        model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}"
        req_body = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2},
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=req_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (urllib.error.URLError, KeyError, IndexError, TimeoutError):
            return None

    return None


def investigate(mesh: MeshNetwork, question: str, selected: str = "F4") -> dict[str, str]:
    m = re.search(r"f\s?([1-8])", question, re.IGNORECASE)
    fid = f"F{m.group(1)}" if m else (selected if selected in mesh.nodes else "F4")
    node = mesh.nodes[fid]

    # Build strict local-knowledge forensic context (partition-aware: only events known to `fid`)
    known_events = [
        {
            "t": mesh.events[eid - 1]["t"],
            "origin": mesh.events[eid - 1]["origin"],
            "kind": mesh.events[eid - 1]["kind"],
            "text": mesh.events[eid - 1]["text"],
        }
        for eid in sorted(node.known_ids)[-20:]
    ]
    context = {
        "queried_node": fid,
        "section": node.config["section"],
        "target_c": node.target,
        "thermistor_reported_c": round(node.reported, 2),
        "kalman_power_estimate_c": round(node.estimate, 2),
        "power_watts": round(node.power_watts, 1),
        "estimated_wear": round(node.observer.estimated_wear, 3),
        "health": node.health,
        "security": node.security,
        "trust_category": trust_category(mesh, fid),
        "peer_trust_scores": {p: round(node.trust_score(p), 2) for p in IDS if p != fid},
        "network_partitioned": mesh.partitioned,
        "rescue_plan": mesh.plan,
        "known_events": known_events,
    }
    prompt = f"Operator question: {question}\nLocal node forensic state:\n{json.dumps(context)}"
    llm_text = _call_llm_provider(prompt)
    if llm_text:
        return {
            "text": llm_text,
            "basis": f"LLM analysis grounded in {len(node.known_ids)} signed events known to {fid}",
        }

    return _deterministic_investigation(mesh, question, selected)


# ---------------------------------------------------------------------------
# FastAPI + WebSocket Server & Hardware Telemetry Endpoints
# ---------------------------------------------------------------------------


class SimulationHub:
    def __init__(self) -> None:
        self.mesh: MeshNetwork = create_mesh(script=False)
        self.running: bool = True
        self.clients: set[WebSocket] = set()
        self.lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)
        await self.send_state(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    def payload(self) -> dict[str, Any]:
        return {
            "type": "state",
            "running": self.running,
            "state": self.mesh.to_dict(),
        }

    async def send_state(self, ws: WebSocket) -> None:
        await ws.send_json(self.payload())

    async def broadcast(self) -> None:
        if not self.clients:
            return
        msg = self.payload()
        stale: list[WebSocket] = []
        for ws in list(self.clients):
            try:
                await ws.send_json(msg)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.clients.discard(ws)

    async def tick_loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_INTERVAL_SECONDS)
            async with self.lock:
                if not self.running:
                    continue
                step_mesh(self.mesh)
                await self.broadcast()

    async def handle_command(self, ws: WebSocket, msg: dict[str, Any]) -> None:
        cmd = msg.get("type")
        async with self.lock:
            if cmd == "set_running":
                self.running = bool(msg.get("running", True))
            elif cmd == "play":
                self.running = True
            elif cmd == "pause":
                self.running = False
            elif cmd == "step":
                step_mesh(self.mesh)
            elif cmd == "restart":
                self.mesh = create_mesh(script=bool(msg.get("script", False)))
                self.running = True
            elif cmd == "spoof_sensor":
                spoof_sensor(self.mesh, str(msg.get("id", "")))
            elif cmd == "bad_setpoint":
                bad_setpoint(self.mesh, str(msg.get("id", "")))
            elif cmd == "open_door":
                open_door(self.mesh, str(msg.get("id", "")))
            elif cmd == "compromise":
                compromise(self.mesh, str(msg.get("id", "")))
            elif cmd == "lie":
                lie(self.mesh)
            elif cmd == "toggle_central":
                toggle_central(self.mesh)
            elif cmd == "set_partition":
                target = bool(msg["partitioned"]) if "partitioned" in msg else not self.mesh.partitioned
                set_partition(self.mesh, target)
            elif cmd == "approve_plan":
                approve_plan(self.mesh)
            elif cmd == "investigate":
                answer = await asyncio.to_thread(
                    investigate,
                    self.mesh,
                    str(msg.get("question", "")),
                    str(msg.get("selected", "F4")),
                )
                await ws.send_json({
                    "type": "investigate_result",
                    "requestId": msg.get("requestId"),
                    "answer": answer,
                })
                return

            await self.broadcast()


hub = SimulationHub()


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(hub.tick_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="FridgeShield Backend", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class InvestigateRequest(BaseModel):
    question: str
    selected: str = "F4"


class HardwareTelemetry(BaseModel):
    reported_temp: float
    power_watts: float
    door_open: bool = False
    ambient_temp: float = AMBIENT_TEMP_C
    true_temp: Optional[float] = None


@app.get("/api/state")
async def get_state() -> dict[str, Any]:
    async with hub.lock:
        return hub.payload()


@app.post("/api/telemetry/{fridge_id}")
async def ingest_hardware_telemetry(fridge_id: str, reading: HardwareTelemetry) -> dict[str, Any]:
    """
    Ingest live sensor & CT-clamp power readings from a real fridge controller.
    Marks that node as hardware-driven so synthetic physics no longer overwrite it.
    """
    fid = fridge_id.upper()
    if fid not in hub.mesh.nodes:
        raise HTTPException(status_code=404, detail=f"Unknown fridge id: {fridge_id}")

    async with hub.lock:
        hub.mesh.plants[fid].external_override = True
        step_node_control_and_physics(hub.mesh, fid, reading.model_dump())
        hub.mesh.plan = compute_rescue_plan(hub.mesh)
        await hub.broadcast()
        node = hub.mesh.nodes[fid]
        return {
            "id": fid,
            "reported": node.reported,
            "estimate": round(node.estimate, 2),
            "compressor": node.compressor,
            "health": node.health,
            "security": node.security,
        }


@app.post("/api/investigate")
async def post_investigate(req: InvestigateRequest) -> dict[str, str]:
    async with hub.lock:
        return await asyncio.to_thread(investigate, hub.mesh, req.question, req.selected)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await hub.connect(ws)
    try:
        while True:
            data = await ws.receive_json()
            if isinstance(data, dict):
                await hub.handle_command(ws, data)
    except WebSocketDisconnect:
        hub.disconnect(ws)