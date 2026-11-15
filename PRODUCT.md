# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary users are supermarket staff monitoring fridges, investigating problems,
and deciding what action is needed to protect chilled stock. The current product
is a hackathon prototype demonstrated to judges, rather than a deployed store system.
The user confirmed this staff-facing purpose and demonstration context during init.

## Product Purpose

FridgeShield makes fridge health, suspicious readings, and responses understandable
in one live dashboard. Staff should be able to identify the affected fridge,
compare sensor readings with an independent estimate, inspect communication and
incident evidence, and understand recommended or applied fixes.

Success in the demonstration means showing the actual behaviour of independent
fridge processes, their communication, local protection, and AI investigation
clearly enough for staff and judges to follow what happened and why.

## Positioning

Fridges run local controllers and exchange signed events directly with neighbours
over WebSockets. A discovery service manages membership; the dashboard gateway
observes and controls the demo without being the relay for peer event exchange.
Gemini adds investigation and limited, fridge-validated fixes to local protection.
The prototype demonstrates these mechanisms; it does not establish superiority
over commercial monitoring systems or guarantee protection against every attack.

## Operating Context

- Browser dashboard backed by React, Vite, Tailwind CSS, Recharts, and Lucide icons.
- Python/FastAPI backend started from the repository root with `python -m backend.main`.
  `python -m backend.live` serves the same application.
- Default local frontend: `http://localhost:5173`; backend: port 8000;
  WebSocket discovery: port 9000. Run one backend instance at a time.
- Each simulated fridge runs in a separate process. The fleet can grow or shrink
  during the demo; the interface must not depend on a fixed set of eight IDs.
- Judge demonstrations include inspecting readings, introducing faults, observing
  local and peer responses, asking the investigator, and reviewing incidents.

## Capabilities and Constraints

- Sensor values and thermodynamics are simulated. The process boundaries and
  WebSocket traffic are real. Do not describe simulated readings as physical hardware.
- Local thermal/Kalman estimation checks temperature readings against model
  predictions and supports protective cooling when the sensor is unreliable.
- A spoofed sensor may continue showing a false fixed value while actual cooling
  changes. Distinguish sensor, estimate, and simulation ground truth explicitly.
- Gossip propagates signed events, suppresses duplicates, and reconciles missed
  events after reconnection. Signatures identify senders; they do not prove readings truthful.
- Discovery is centralised. Do not claim the entire system is decentralised or that
  membership repair remains available without discovery.
- The current dashboard supports fleet management, neighbour topology, temperature
  history, sensor spoofing, target override, door faults, isolation/reconnection,
  pause/resume, and an optional fridge soundscape.
- Gemini investigation and incident reports use evidence gathered from the fridges.
  Supported fixes are limited commands such as closing the door, protective mode,
  and restoring a safe target; the receiving fridge checks and applies them.
  Explain a proposed, pending, applied, or rejected fix according to its reported state.
- Local safety continues without Gemini. AI availability must not imply that local
  protection is available or unavailable, and AI answers must not masquerade as telemetry.
- Gemini needs server-side `GEMINI_API_KEY` and `GEMINI_MODEL` configuration in
  `backend/.env`. Never expose these secrets in frontend code or product documentation.
- Preserve existing live controls, AI conversations, reports, network evidence,
  isolation behaviour, and soundscape when refining the interface.
- Hardware deployment, production security guarantees, regulatory compliance,
  and commercial readiness are not established by this prototype.

## Brand Commitments

The product name is FridgeShield. Use concise language suitable for supermarket
staff and a live judge demonstration. Explain system behaviour concretely and
distinguish observed evidence from an AI interpretation.

The user's existing dashboard brief references Satnaing's Shadcn Admin template,
asks for the shadcn/Tailwind design package, restrained use of Slate, and a small
amount of thematic colour. These are user-supplied constraints, not permission
to invent a new visual direction during init.

## Evidence on Hand

- `backend/distributed.py`: independent node transport, discovery, signing, gossip,
  and reconciliation.
- `backend/edge.py` and `backend/thermal.py`: local controller and simulated sensors.
- `backend/live.py`, `backend/main.py`, and investigator/report modules: gateway,
  investigation, limited fixes, and incident reporting.
- `frontend/src/LiveDashboard.jsx`, `AiInvestigator.jsx`, `NetworkFeed.jsx`, and
  `IncidentReport.jsx`: incumbent live interface and workflows.
- `README.md` and `backend/DISTRIBUTED.md`: setup and protocol documentation.
- Backend tests provide implementation checks, not food-safety certification.
- No confirmed customer testimonials, measured food-waste savings, security
  certification, or field-deployment results have been supplied. Do not fabricate them.

## Product Principles

1. Make the affected fridge, evidence, and current response easy to identify.
2. Keep simulated truth, sensor claims, estimates, and AI advice distinguishable.
3. Preserve local protection and explain degraded connectivity or unavailable AI.
4. Show what fridges actually communicated and whether fixes were applied.
5. Demonstrate the prototype honestly without overstating security or production readiness.

## Open Decisions

Production deployment, physical hardware integration, additional user roles, and
product-specific accessibility certification requirements are not yet confirmed.
