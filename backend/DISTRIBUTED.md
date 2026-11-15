# Distributed fridge communication

`distributed.py` runs each fridge as an independent process with private state.
All communication uses WebSockets. Fridges connect directly to neighbours;
discovery announces membership and public keys, and never relays fridge events.

The live dashboard now integrates this transport: run `python -m backend.live`
and `npm run dev` in `frontend/` (see the root README). The gateway starts discovery
and four separate simulated fridge processes and streams their state to the UI.
Add/remove controls scale the local demo. `--simulate` enables the shared thermal
and Kalman models and local fault detection, with rescue requests and automatic
compatible-space offers learned through gossip. Stock transfers remain advisory.
The original `backend.py` prototype's trust/LLM/scenario logic is separate.
`/control` supplies local events, commands and inspection over WebSockets.

## Run on one laptop

From the repository root (`FridgeSheild-Durhack-2026`), install in a virtual
environment (or your chosen Python environment):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend/requirements.txt
```

Activate that environment in each terminal. Start discovery in terminal 1:

```powershell
python -m backend.distributed discovery --port 9000
```

Start one fridge in each additional terminal:

```powershell
python -m backend.distributed node --id F1 --port 9001
```

```powershell
python -m backend.distributed node --id F2 --port 9002
```

```powershell
python -m backend.distributed node --id F3 --port 9003
```

Send a request to F1 from another terminal. The default `--url` is F1's
`ws://127.0.0.1:9001/control`. `--data` must be a JSON object; you can omit it
for a simple propagation test:

```powershell
python -m backend.distributed emit --kind rescue_request
python -m backend.distributed state --url ws://127.0.0.1:9003/control
```

F3's `events` should include the signed request originating from F1. Other kinds
are `telemetry`, `alert`, and `capacity_offer`. From a WebSocket client, publish
actual readings using this message to the local fridge's `/control` socket:

```json
{"type":"emit","kind":"telemetry","data":{"reported_temp":3.2,"power_watts":450,"door_open":false}}
```

Read local state with `{"type":"state"}`. This is request/response over a
WebSocket, not an HTTP API. State includes configured neighbours, connected
neighbours, membership, received events, vector-clock counters and rejection count.

## Scale up and down

Add a fridge at any time, with a unique ID and unused port:

```powershell
python -m backend.distributed node --id F4 --port 9004
```

Stop a fridge with Ctrl+C. Discovery removes disconnected members and repairs
the topology automatically. Abrupt failures are detected through WebSocket
ping/pong timeouts (3-second interval and 3-second timeout, plus scheduling and
reconnect time). A restarted fridge gets a new session ID so its sequence numbers
cannot collide with its previous incarnation. An active ID cannot register twice.

Neighbours are the predecessor and successor in the sorted ring of active IDs.
For example, with four fridges:

```text
F1 —— F2
|      |
F4 —— F3
```

F1 reaches F3 via gossip through F2 or F4; there is no direct F1–F3 socket.
One member has no neighbours; two members have one neighbour each; three or
more have two each. There is no hardcoded fridge count or F1–F8 list. Ring
neighbours are logical neighbours, not physical floor-plan proximity.

## Communication algorithms

- **Gossip:** accepted events become available to neighbours, which forward them
  onwards. Each node keeps its own grow-only event set and ignores duplicates.
- **Anti-entropy:** peers exchange event-ID summaries on connection and periodically
  (roughly once per second), then send the events the other side lacks. A new node
  catches up, and disconnected peers recover missed events after reconnecting.
- **Vector clocks:** each node tracks the greatest sequence observed per origin
  session. Exact event IDs are also reconciled, because a maximum sequence alone
  would miss holes caused by out-of-order delivery.
- **Signed events:** each process generates a random Ed25519 key. Discovery shares
  the public keys; peers prove key ownership through a signed random challenge.
  Forwarded events retain their origin signature. Invalid signatures are rejected;
  conflicting signed events with the same session/sequence are logged and rejected
  after the first accepted version. This is detection, not Byzantine consensus or
  automatic quarantine; a valid signature cannot prove a temperature is truthful.
- **Membership repair:** discovery broadcasts an updated ring on join/leave.
  Existing neighbour sockets continue working during a discovery outage, but
  joining and automatic topology repair require discovery to be available.

These reuse the original backend's gossip, event-log, signature and vector-clock
ideas with actual network transport, rather than calling `gossip_round()` on
objects held in one shared `MeshNetwork`.

## Multiple machines and current limits

For a trusted LAN, bind discovery to `--host 0.0.0.0`. On each fridge, set
`--host 0.0.0.0 --advertise-host <this-fridge-LAN-IP>` and
`--discovery ws://<discovery-LAN-IP>:9000`. Allow the selected ports through the
firewall. Use the LAN IP, not `0.0.0.0`, as the advertised address.

Peer/discovery defaults bind to localhost and reject browser-origin connections.
The gateway's `/ws` allows the local Vite frontend (port 5173); configure
`FRIDGE_UI_ORIGINS` for other frontend origins and `VITE_BACKEND_WS` for a deployed
gateway URL. Vite proxies `/ws` and `/api` to port 8000 during development. Production
hosting must provide equivalent reverse proxies or set the frontend WebSocket URL.
The gateway is a monitoring/control client, not an event relay between fridges.
This is a
hackathon transport prototype: membership and `/control` do not authenticate
operators, and traffic uses unencrypted `ws://`. Keep it on a trusted network;
remote deployment needs authenticated discovery, protected control access,
provisioned identities and TLS. Keys and event logs are currently in memory,
so complete cluster shutdown loses history. Surviving nodes can restore a
restarting member's history. The discovery service also retains departed-session
public keys until it exits. Logs and summaries grow without compaction and each
frame is limited to 1 MiB, so large/long-running fleets need persistence,
pagination and log-retention work. This supports dynamic membership for the demo,
not unlimited fleet size.

## Verify

```powershell
python -m unittest backend.test_distributed -v
```

Tests use real localhost WebSockets. They cover multi-hop delivery, membership
changes, restart, reconnection, out-of-order gaps, duplicate suppression,
signature rejection, discovery outage, and communication between separate Python
processes.
