"""Automatic incident explanations.

Every few seconds the watcher checks each fridge's security, target and door. When a fridge
develops a new problem, the watcher asks that fridge for its own view and has the
investigator explain it once, so staff see an explanation without having to ask.

The fridges never wait for this. If Gemini is slow, rate-limited or switched off, the
store carries on exactly the same; only the explanations stop.

Calls are rationed for the Gemini free tier:
  - at most one explanation per fridge per AI_INCIDENT_COOLDOWN seconds (default 60)
  - at least AI_MIN_GAP seconds between any two calls (default 12)
Set AI_AUTO_INCIDENTS=0 in backend/.env to switch it off entirely.

If the AI asks for a fix (like closing a door), `act` sends it to the fridge, which
checks it and carries it out itself. If the fridge can't be reached, its own local
reflexes still handle the problem.
"""
import asyncio
import logging
import math
import os
import time

from .ai_context import build_packet
from .investigator import investigate

LOG = logging.getLogger("fridge.ai")
CHECK_EVERY_SECONDS = 3


def concern(local):
    """A short description of what's wrong with a fridge, or None if it's fine."""
    if not local:
        return None
    parts = []
    if local.get("security") not in (None, "normal"):
        parts.append(f"security {local['security']}")
    if local.get("target_overridden"):
        parts.append(f"target changed to {local.get('target'):g}°C")
    if local.get("door"):
        parts.append("door open")
    return ", ".join(parts) or None


class IncidentWatcher:
    def __init__(self, fetch_state, ask=investigate, act=None, clock=time.monotonic):
        self.fetch_state = fetch_state  # async: fridge id -> that fridge's own state
        self.ask = ask
        self.act = act  # async: (fridge id, actions) -> results, or None to only advise
        self.clock = clock
        self.last_concern = {}
        self.pending = {}  # fridge id -> concern waiting for an explanation
        self.last_call_for = {}
        self.last_call = -math.inf
        self.incidents = []

    @property
    def enabled(self):
        return os.getenv("AI_AUTO_INCIDENTS", "1") != "0" and bool(os.getenv("GEMINI_API_KEY"))

    def public(self):
        """Recent incidents for the dashboard stream (without the long prompt text)."""
        return [{k: v for k, v in incident.items() if k != "prompt"} for incident in self.incidents[-20:]]

    def observe(self, nodes):
        """Compare each fridge's state with last time and note new problems."""
        for fid, state in nodes.items():
            now_concern = concern(state.get("local"))
            before = self.last_concern.get(fid)
            self.last_concern[fid] = now_concern
            if now_concern and now_concern != before:
                self.pending[fid] = now_concern
            elif not now_concern and before:
                self.pending.pop(fid, None)
                for incident in self.incidents:
                    if incident["fridge_id"] == fid and incident["resolved_at"] is None:
                        incident["resolved_at"] = time.time()

    async def step(self):
        """Explain at most one pending incident, if the rate limits allow."""
        if not self.enabled or not self.pending:
            return None
        now = self.clock()
        if now - self.last_call < float(os.getenv("AI_MIN_GAP", "12")):
            return None
        cooldown = float(os.getenv("AI_INCIDENT_COOLDOWN", "60"))
        for fid, what in list(self.pending.items()):
            if now - self.last_call_for.get(fid, -math.inf) < cooldown:
                continue
            try:
                state = await self.fetch_state(fid)
            except Exception as exc:
                # Probably cut off from the network: keep it pending and explain once it's back.
                LOG.info("Can't reach %s for an incident explanation yet: %s", fid, exc)
                continue
            del self.pending[fid]
            self.last_call = self.last_call_for[fid] = now
            question = (f"{fid} has just raised an alert ({what}). Explain to the member of staff on shift "
                        f"what is most likely happening and what they should do now.")
            result = await self.ask(question, build_packet(state))
            actions = (result.get("diagnosis") or {}).get("actions") or []
            if self.act and actions:
                result["actions_taken"] = await self.act(fid, actions)
            incident = {"fridge_id": fid, "concern": what, "created_at": time.time(), "resolved_at": None, **result}
            self.incidents = (self.incidents + [incident])[-50:]
            return incident
        return None

    async def run(self, fleet_nodes):
        while True:
            try:
                self.observe(await fleet_nodes())
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Incident watcher error")
            await asyncio.sleep(CHECK_EVERY_SECONDS)
