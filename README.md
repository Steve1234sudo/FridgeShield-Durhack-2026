# FridgeShield-Durhack-2026

## Run the live dashboard

From the repository root, in a Python 3.11+ environment:

```powershell
python -m pip install -r backend/requirements-distributed.txt
python -m backend.live
```

This starts the dashboard gateway on `127.0.0.1:8000`, discovery on port 9000,
and four independent fridge processes. Each fridge simulates its own sensors,
runs the shared thermal/Kalman models and exchanges signed events directly with
its neighbours over WebSockets.

In a second terminal:

```powershell
cd frontend
npm install
npm run dev
```

Open **http://localhost:5173**. The frontend receives live backend snapshots;
it does not generate its own readings. Add/remove fridges, select nodes to see
temperature history and peer connections, and inject door, wear or sensor faults.
Rescue requests and capacity offers appear in the event feed. Offers are advisory,
not approved transfers. Disconnected gateway data is labelled stale.

Use `FRIDGE_COUNT` to change the initial fleet size (default 4, local demo maximum
32 managed processes). Additional independently launched nodes can join discovery
and appear in the UI. Only gateway-managed nodes can be removed using its button.
Stop the backend with Ctrl+C to clean up its child fridges. Don't run another
discovery service on port 9000 at the same time.

The original fixed-eight-fridge prototype remains in `backend/backend.py` and
`frontend/src/FridgeShieldDashboard.jsx`; the live app uses `backend/live.py` and
`frontend/src/LiveDashboard.jsx`. Physical modelling is shared in `backend/thermal.py`.
The prototype's cloud investigator, central lockout and global partition scenario
are not controls in the new distributed dashboard. Membership remains discovery-
based, and signatures aren't proof that sensor readings are truthful.

For protocol details, manual node commands, and current demo limits, see
[the distributed communication guide](backend/DISTRIBUTED.md).

## Checks

```powershell
python -m unittest backend.test_distributed backend.test_live -v
cd frontend
npm run build
```
