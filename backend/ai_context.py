"""What the AI is allowed to see: one fridge's own view of the store.

The investigator never reads a central database. It asks a single fridge for its
state over that fridge's control socket (the same way the dashboard does) and builds
the situation packet from what that fridge knows, including what it has heard from
other fridges through gossip. During a partition it therefore sees only that fridge's
side of the store.

Simulator ground truth (the cabinet's true temperature, whether the sensor is really
being spoofed) is deliberately left out, so the
AI has to reason from evidence exactly like the fridges do.
"""
import re

NOTABLE_KINDS = {"alert", "ack"}  # telemetry is too noisy to send
MAX_EVENTS = 20
RECENT_READINGS = 12
CHILLED_LEGAL_LIMIT_C = 8  # UK chilled food limit
FRIDGE_REF = re.compile(r"\bf(?:ridge)?\s*(\d+)\b", re.IGNORECASE)


def mentioned_fridge(question, members):
    """The first fridge named in a question ("F3", "fridge 3"), if it's in the store."""
    for match in FRIDGE_REF.finditer(question):
        fid = f"F{match.group(1)}"
        if fid in members:
            return fid
    return None


def _round(value, digits=1):
    return round(value, digits) if isinstance(value, (int, float)) and not isinstance(value, bool) else value


def describe_event(event):
    data, origin, kind = event.get("data", {}), event.get("origin"), event.get("kind")
    if kind == "alert":
        if data.get("rejected"):
            return f"{origin} rejected a remote command: {data.get('reason')}"
        if data.get("action"):
            return f"{origin} carried out {data['action']}: {data.get('reason')}"
        if data.get("fault") in ("network", "blocked"):
            return f"{origin}: {data.get('reason')}"
        if data.get("fault") == "target":
            return f"{origin} reported: {data.get('reason')}"
        if data.get("security"):
            return f"{origin} changed its security state to {data['security']}: {data.get('reason')}"
        return f"{origin} reported: {data.get('reason')}"
    if kind == "ack":
        decision = {"distrust_sensor": f"stopped trusting {data.get('to')}'s temperature readings",
                    "trust_restored": f"trusts {data.get('to')}'s readings again"}.get(data.get("verdict"), "noted it")
        own = "its own readings are normal" if data.get("fine") else "it is seeing problems too"
        tip = f" Advice: {data['advice']}." if data.get("advice") else ""
        return f"{origin} heard {data.get('to')}'s warning, checked itself ({own}) and {decision}.{tip}"
    if kind == "capacity_offer":
        return f"{origin} offered {data.get('litres')} L of spare space to {data.get('for')}"
    return f"{origin} sent a {kind} event"


def build_packet(state):
    """Turn one fridge's /control state reply into the investigator's situation packet."""
    local = state.get("local") or {}
    neighbours = state.get("neighbours", [])
    connected = state.get("connected", [])

    fridge = {
        "id": state.get("id"),
        "target_c": local.get("target"),
        "safe_target_c": local.get("safe_target"),
        "target_overridden": local.get("target_overridden"),
        "frozen": local.get("frozen"),
        "sensor_reading_c": _round(local.get("reported")),
        "independent_model_estimate_c": _round(local.get("model_estimate")),
        "sensor_vs_model_gap_c": _round(local.get("sensor_gap")),
        "temperature_estimate_in_use_c": _round(local.get("estimate")),
        "cooling_controlled_by": local.get("control_source"),
        "compressor_on": local.get("compressor"),
        "compressor_power_w": _round(local.get("power_watts"), 0),
        "door_open": local.get("door"),
        "defrost_cycle_active": local.get("defrost"),
        "door_open_minutes": local.get("door_open_minutes"),
        "neighbours_whose_readings_it_distrusts": local.get("distrusted_sensors", []),
        "security": local.get("security"),
        "paused": local.get("paused"),
    }
    if not local.get("frozen"):
        fridge["legal_limit_c"] = CHILLED_LEGAL_LIMIT_C

    history = local.get("history", [])
    recent = history[-RECENT_READINGS:]
    sensor = [h["reported"] for h in recent if h.get("reported") is not None]
    estimate = [h["estimate"] for h in recent if h.get("estimate") is not None]
    peak = max((h["estimate"] for h in history if h.get("estimate") is not None), default=None)
    # The sensor and the cabinet's real temperature are different things. Spelling both out
    # stops the AI describing a stuck sensor reading as if it were the fridge's temperature.
    temperatures = {
        "sensor_now_c": _round(local.get("reported")),
        "sensor_range_last_12_min_c": [_round(min(sensor)), _round(max(sensor))] if sensor else None,
        "sensor_looks_stuck": bool(sensor) and len(sensor) >= 5 and max(sensor) - min(sensor) < 0.4,
        "real_temp_estimate_now_c": _round(local.get("model_estimate")),
        "real_temp_estimate_range_last_12_min_c": [_round(min(estimate)), _round(max(estimate))] if estimate else None,
        "real_temp_estimate_peak_last_hour_c": _round(peak),
        "note": f"The estimate comes from the {local.get('estimate_source') or 'thermal model'}, independent of the "
                "sensor. During a defrost cycle the fridge warms on purpose; that is normal.",
    }

    readings = [
        {"tick": h.get("t"), "sensor_c": _round(h.get("reported")), "estimate_c": _round(h.get("estimate"))}
        for h in local.get("history", [])[-RECENT_READINGS:]
    ]

    notable = [e for e in state.get("events", []) if e.get("kind") in NOTABLE_KINDS][-MAX_EVENTS:]
    events = [
        {"id": i, "from": e.get("origin"), "kind": e.get("kind"), "text": describe_event(e)}
        for i, e in enumerate(notable, start=1)
    ]

    network = {
        "neighbours": neighbours,
        "connected_now": connected,
        "neighbours_unreachable": sorted(set(neighbours) - set(connected)),
        "fridges_in_store": state.get("members", []),
        "membership_service_online": state.get("discovery_online"),
        "events_known": state.get("event_count"),
        "messages_rejected_for_bad_signatures": state.get("rejected"),
    }

    return {
        "viewpoint": f"Everything below is what {fridge['id']} itself knows. Other fridges may know things it hasn't heard yet.",
        "fridge": fridge,
        "temperatures": temperatures,
        "recent_readings": readings,
        "network": network,
        "events": events,
    }
