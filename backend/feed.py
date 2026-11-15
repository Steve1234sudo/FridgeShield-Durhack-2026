"""A plain-English network feed, built from what the fridges actually sent each other.

Every fridge records which neighbour delivered each message and when, and neighbours
reply to warnings with a decision and one piece of advice. Each feed item reads like:

    F1 reported a possible sensor fault to F2 and F3.
    F2 and F3 stopped trusting F1's temperature readings.
    Advice: Check F1 with a handheld thermometer.

Nothing here is invented by the gateway; it only reads the fridges' own records.
"""
import time

FEED_KINDS = {"alert"}  # replies ("ack") are attached to their warning

ACTION_TEMPLATES = {
    "close_door": ("{f} told {to} the AI closed its door", "fixed"),
    "auto_close_door": ("{f} closed its own open door and told {to}", "fixed"),
    "protective_mode": ("{f} told {to} it switched to protective mode at the AI's request", "fixed"),
    "restore_target": ("{f} told {to} the AI restored its safe target temperature", "fixed"),
    "auto_restore_target": ("{f} restored its own safe target before the food got too warm, and told {to}", "fixed"),
    "safe_mode_restore": ("{f} went into safe mode when cut off and restored its safe target, and told {to}", "fixed"),
}


def names(fridges):
    fridges = list(fridges)
    return fridges[0] if len(fridges) == 1 else f"{', '.join(fridges[:-1])} and {fridges[-1]}"


def template(event):
    """A sentence template with {f} (the reporting fridge) and {to} (who heard it), plus a tone."""
    data, kind = event.get("data", {}), event["kind"]
    if kind == "alert":
        if data.get("rejected"):
            return "{f} blocked an unsafe remote command and warned {to}", "warning"
        if data.get("action"):
            return ACTION_TEMPLATES.get(data["action"], ("{f} told {to} it carried out " + data["action"], "fixed"))
        if data.get("fault") == "network":
            return (("{f} reconnected and synced with {to}", "good") if data.get("resolved")
                    else ("{f} was cut off from the network, and {to} heard once it was back", "warning"))
        if data.get("fault") == "blocked":
            attack = {"override_target": "a target override", "spoof_sensor": "a sensor spoofing attempt",
                      "bad_setpoint": "an unsafe setpoint"}.get(data.get("command"), "a remote attack")
            return "{f} blocked " + attack + " while it was cut off, and told {to}", "good"
        if data.get("fault") == "target":
            if data.get("resolved"):
                return "{f} told {to} its target temperature is back to normal", "good"
            return "{f} warned {to} that its target temperature was changed to " + f"{data.get('target'):g}°C", "warning"
        security = data.get("security")
        if security == "suspicious":
            return "{f} reported a possible sensor fault to {to}", "warning"
        if security == "protective":
            return "{f} told {to} it has stopped trusting its own sensor", "warning"
        if security == "normal":
            return "{f} told {to} its sensor is working again", "good"
        if data.get("reason") == "Door opened":
            return "{f} reported an open door to {to}", "warning"
        return "{f} sent {to} an alert: " + str(data.get("reason", "")), "info"
    return "{f} sent {to} a " + kind + " message", "info"


def outcome(fridge, replies):
    """One sentence on what the neighbours decided after checking their own readings."""
    distrust = [r["fridge"] for r in replies if r.get("verdict") == "distrust_sensor"]
    restored = [r["fridge"] for r in replies if r.get("verdict") == "trust_restored"]
    checked = [r["fridge"] for r in replies if r.get("verdict") == "noted" and r.get("fine")]
    targets_ok = [r["fridge"] for r in replies if r.get("verdict") == "target_checked" and r.get("fine")]
    targets_hit = [r["fridge"] for r in replies if r.get("verdict") == "target_checked" and not r.get("fine")]
    trouble = [r["fridge"] for r in replies if r.get("verdict") != "target_checked" and not r.get("fine")]
    parts = []
    if distrust:
        parts.append(f"{names(distrust)} stopped trusting {fridge}'s temperature readings")
    if restored:
        parts.append(f"{names(restored)} trust {fridge}'s readings again")
    if checked:
        parts.append(f"{names(checked)} checked {'its' if len(checked) == 1 else 'their'} own readings: all normal")
    if targets_ok:
        parts.append(f"{names(targets_ok)} checked {'its' if len(targets_ok) == 1 else 'their'} own target: unchanged")
    if targets_hit:
        parts.append(f"{names(targets_hit)} found {'its' if len(targets_hit) == 1 else 'their'} target was changed too")
    if trouble:
        parts.append(f"{names(trouble)} {'is' if len(trouble) == 1 else 'are'} seeing problems too")
    return "; ".join(parts) + "." if parts else ""


def advice(replies):
    """One piece of advice: from a neighbour seeing trouble if any (store-wide), else the first given."""
    given = [r for r in replies if r.get("advice")]
    worried = [r for r in given if not r.get("fine")]
    return (worried or given or [{}])[0].get("advice", "")


def build_feed(nodes, limit=25):
    events, created, heard, replies = {}, {}, {}, {}
    for fid, state in nodes.items():
        for e in state.get("events", []):
            events[f"{e['session']}:{e['seq']}"] = e
        for a in state.get("arrivals", []):
            if a["via"] is None:
                created[a["event"]] = a["at"]
            else:
                heard.setdefault(a["event"], {}).setdefault(fid, a)  # first delivery wins
    for eid, e in events.items():
        if e["kind"] == "ack":
            parent = f"{e['data'].get('for_session')}:{e['data'].get('for_seq')}"
            replies.setdefault(parent, []).append((eid, e))

    items = []
    for eid, e in events.items():
        if e["kind"] not in FEED_KINDS:
            continue
        deliveries = heard.get(eid, {})
        start = created.get(eid) or min((a["at"] for a in deliveries.values()), default=None)
        if start is None:
            continue
        heard_by = sorted(({"fridge": fid, "via": a["via"], "direct": a["via"] == e["origin"],
                            "after_ms": max(0, round((a["at"] - start) * 1000))}
                           for fid, a in deliveries.items()), key=lambda h: h["after_ms"])
        reply_list = [{"fridge": r["origin"], "fine": r["data"].get("fine"), "verdict": r["data"].get("verdict"),
                       "advice": r["data"].get("advice", ""), "text": r["data"].get("note", ""),
                       "after_ms": max(0, round((created.get(rid, start) - start) * 1000))}
                      for rid, r in replies.get(eid, [])]
        # Neighbours that replied were reached too, even if a relayed copy happened to arrive first.
        direct = [h["fridge"] for h in heard_by if h["direct"]]
        direct += [r["fridge"] for r in reply_list if r["fridge"] not in direct]
        sentence, tone = template(e)
        text = sentence.format(f=e["origin"], to=names(direct) if direct else "its neighbours")
        if not direct and e["data"].get("fault") == "network":
            text = f"{e['origin']} was cut off from the network"  # neighbours hear about it once it's back
        elif not direct:
            text += ", but none could be reached"
        tip = advice(reply_list)
        items.append({
            "id": eid, "at": start, "fridge": e["origin"], "tone": tone,
            "text": text + ".", "outcome": outcome(e["origin"], reply_list),
            "advice": f"Advice: {tip}." if tip else "",
            "heard_by": heard_by, "replies": reply_list,
        })
    items.sort(key=lambda i: i["at"])
    return items[-limit:]


def active_links(nodes, window=1.5):
    """Messages delivered between neighbours in the last moment, for animating the network graph."""
    now = time.time()
    return [{"from": a["via"], "to": fid, "kind": a["kind"]}
            for fid, state in nodes.items() for a in state.get("arrivals", [])
            if a["via"] is not None and now - a["at"] < window]
