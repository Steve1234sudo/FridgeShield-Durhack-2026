import { useEffect, useRef, useState } from "react";
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer } from "recharts";
import { ShieldCheck, Plus, Trash2, Zap, DoorOpen, Activity, Pause } from "lucide-react";
import AiInvestigator from "./AiInvestigator";

const button = "inline-flex items-center gap-2 rounded-lg border border-slate-700 px-3 py-2 text-sm hover:bg-slate-800 disabled:opacity-40";
const panel = "rounded-xl border border-slate-800 bg-slate-900 p-5";
const temperature = (value) => Number.isFinite(value) ? `${value.toFixed(1)}°C` : "—";

export default function LiveDashboard() {
  const socket = useRef(null);
  const [data, setData] = useState(null);
  const [status, setStatus] = useState("connecting");
  const [selected, setSelected] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [truth, setTruth] = useState(false);
  useEffect(() => {
    let stopped = false;
    let retry;
    let active;
    const open = () => {
      const url = import.meta.env.VITE_BACKEND_WS || `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`;
      active = new WebSocket(url);
      socket.current = active;
      active.onopen = () => { setStatus("connected"); setError(""); };
      active.onmessage = ({ data: raw }) => {
        const msg = JSON.parse(raw);
        if (msg.type === "state") { setData(msg); setStatus("connected"); }
        if (msg.type === "error") { setError(msg.message); setBusy(false); }
        if (msg.type === "result") {
          setBusy(false);
          setNotice(msg.result.reason || (msg.result.id ? `Added ${msg.result.id}` : "Command delivered to backend"));
        }
      };
      active.onclose = () => {
        if (stopped) return;
        setStatus("disconnected"); setBusy(false);
        retry = setTimeout(open, 1500);
      };
      active.onerror = () => active.close();
    };
    open();
    return () => { stopped = true; clearTimeout(retry); active?.close(); };
  }, []);

  const command = (name, id) => {
    if (socket.current?.readyState !== WebSocket.OPEN) return;
    setError(""); setNotice(""); setBusy(true);
    socket.current.send(JSON.stringify({ command: name, id, requestId: crypto.randomUUID() }));
  };
  const nodes = Object.values(data?.nodes || {});
  const chosen = data?.nodes[selected] || nodes[0];
  const local = chosen?.local;
  const ids = nodes.map((n) => n.id);
  const radius = Math.max(100, nodes.length * 12);
  const width = radius * 2 + 160;
  const positions = Object.fromEntries(ids.map((id, i) => {
    const angle = i * 2 * Math.PI / ids.length - Math.PI / 2;
    return [id, { x: width / 2 + radius * Math.cos(angle), y: width / 2 + radius * Math.sin(angle) }];
  }));
  const online = status === "connected";
  const events = (data?.events || []).filter((e) => e.kind !== "telemetry").slice(-30).reverse();
  const requests = events.filter((e) => e.kind === "rescue_request");
  const offers = events.filter((e) => e.kind === "capacity_offer");

  return <main className="min-h-screen bg-slate-950 p-4 text-slate-100 md:p-7">
    <div className="mx-auto max-w-7xl space-y-5">
      <header className="flex flex-wrap items-center gap-3">
        <ShieldCheck className="text-sky-400" /><h1 className="text-2xl font-semibold">FridgeShield</h1>
        <span className={`rounded-full px-3 py-1 text-xs ${online ? "bg-emerald-950 text-emerald-300" : "bg-amber-950 text-amber-300"}`}>{status}</span>
        <span className="text-sm text-slate-400">Live backend · simulated sensors</span>
        <button className={`${button} ml-auto`} disabled={!online || busy} onClick={() => command("add")}><Plus size={16} />Add fridge</button>
      </header>
      {!online && <div role="status" className={`${panel} border-amber-800 text-amber-200`}>Waiting for backend. Start <code>python -m backend.live</code> from the repository root. {data && "Displayed readings are stale until reconnection."}</div>}
      {error && <div role="alert" className="rounded-lg bg-red-950 p-3 text-red-200">{error}</div>}
      {notice && <p role="status" className="text-sm text-sky-300">{notice}</p>}
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        {[ ["Active fridges", nodes.length], ["Direct peer links", data?.links.length || 0],
          ["Need attention", nodes.filter((n) => n.local && (n.local.health !== "normal" || n.local.security !== "normal")).length],
          ["Unreachable", data?.unreachable.length || 0] ].map(([label, value]) => <div className={panel} key={label}><p className="text-sm text-slate-400">{label}</p><p className="mt-2 text-3xl font-semibold">{value}</p></div>)}
      </div>
      <div className="grid gap-5 lg:grid-cols-2">
        <section className={panel}>
          <h2 className="text-lg font-medium">Neighbour network</h2><p className="text-sm text-slate-400">Lines show actual connected WebSocket peers. Select a fridge.</p>
          {nodes.length ? <svg className="max-h-96 w-full" viewBox={`0 0 ${width} ${width}`} role="img" aria-label="Live fridge network">
            {data.links.map(([a, b]) => <line key={`${a}-${b}`} x1={positions[a].x} y1={positions[a].y} x2={positions[b].x} y2={positions[b].y} stroke="#334155" strokeWidth="3" />)}
            {nodes.map((node) => <g key={node.id} role="button" tabIndex={0} aria-label={`Select ${node.id}`} onClick={() => setSelected(node.id)} onKeyDown={(e) => { if (e.key === "Enter") setSelected(node.id); }} className="cursor-pointer">
              <circle cx={positions[node.id].x} cy={positions[node.id].y} r="30" fill={node.local?.security === "protective" ? "#0c4a6e" : node.local?.health === "failing" ? "#7f1d1d" : "#064e3b"} stroke={chosen?.id === node.id ? "#e2e8f0" : "#34d399"} strokeWidth="2" />
              <text x={positions[node.id].x} y={positions[node.id].y - 3} textAnchor="middle" fill="#f1f5f9" fontSize="13">{node.id}</text>
              <text x={positions[node.id].x} y={positions[node.id].y + 13} textAnchor="middle" fill="#cbd5e1" fontSize="10">{temperature(node.local?.security === "protective" ? node.local?.estimate : node.local?.reported)}</text>
            </g>)}
          </svg> : <p className="py-16 text-center text-slate-400">No fridges online. Add one to start.</p>}
          <div className="flex flex-wrap gap-2">{ids.map((id) => <button className={button} key={id} onClick={() => setSelected(id)}>{id}</button>)}</div>
        </section>
        <section className={panel}>
          <div className="flex items-center justify-between"><h2 className="text-lg font-medium">{chosen ? `Fridge ${chosen.id}` : "Fridge details"}</h2>
            {chosen && <button className={button} disabled={!online || busy || !data.managed.includes(chosen.id)} onClick={() => command("remove", chosen.id)}><Trash2 size={16} />Remove</button>}
          </div>
          {chosen && <>
            <p className="mt-2 text-sm text-slate-400">Neighbours: {chosen.neighbours.join(", ") || "none"} · Connected: {chosen.connected.join(", ") || "none"}</p>
            <p className="text-sm text-slate-400">{Object.keys(chosen.clock).length} origin sessions · {chosen.event_count} known events · {chosen.rejected} rejected</p>
            {local?.reported !== undefined ? <>
              {local.spoofed && <p className="mt-3 rounded-lg bg-amber-950 p-3 text-sm text-amber-200">Sensor spoofing is active: the sensor reports a fake value near {temperature(local.target - 1)}, even while the fridge cools. {local.security === "protective" ? "Protective mode controls cooling from the independent model estimate." : "The controller is checking the sensor against its independent thermal model."} Use the estimate and simulation ground truth to inspect cooling.</p>}
              <p className="mt-3 text-xs text-slate-400">Cooling control: {local.control_source || "sensor"} · Sensor/model gap: {temperature(local.sensor_gap)} · Blue chart line: sensor · Pink: estimate</p>
              <div className="my-4 grid grid-cols-2 gap-3 text-sm">
                {[["Sensor", temperature(local.reported)], ["Estimate", temperature(local.estimate)], ["Target", temperature(local.target)], ["Power", `${local.power_watts.toFixed(0)} W`], ["Health", local.health], ["Security", local.security], ["Compressor", local.compressor ? "on" : "off"], ["Stock", `${local.stock} / ${local.capacity} L`]].map(([k, v]) => <div className="flex justify-between gap-2 rounded bg-slate-800 p-2" key={k}><span className="text-slate-400">{k}</span><span>{v}</span></div>)}
              </div>
              <div className="h-48"><ResponsiveContainer width="100%" height="100%"><LineChart data={local.history}>
                <XAxis dataKey="t" stroke="#64748b" /><YAxis stroke="#64748b" domain={["auto", "auto"]} /><Tooltip contentStyle={{ background: "#0f172a", border: "1px solid #334155" }} />
                <Line dataKey="reported" name="Sensor °C" stroke="#38bdf8" dot={false} isAnimationActive={false} />
                <Line dataKey="estimate" name="Estimate °C" stroke="#fb7185" dot={false} isAnimationActive={false} />
                {truth && <Line dataKey="actual" name="Actual °C" stroke="#d879ef" dot={false} isAnimationActive={false} />}
              </LineChart></ResponsiveContainer></div>
              <label className="text-xs text-slate-400"><input type="checkbox" checked={truth} onChange={(e) => setTruth(e.target.checked)} /> Show simulation ground truth</label>
              <div className="mt-4 flex flex-wrap gap-2">{[["spoof_sensor", local.spoofed ? "Stop spoofing" : "Spoof sensor", Zap], ["open_door", local.door ? "Close door" : "Open door", DoorOpen], ["wear", local.wearing ? "Stop wear" : "Simulate wear", Activity], ["pause", local.paused ? "Resume" : "Pause", Pause], ["bad_setpoint", "Send unsafe setpoint", ShieldCheck]].map(([name, label, Icon]) => <button className={button} disabled={!online || busy} key={name} onClick={() => command(name, chosen.id)}><Icon size={14} />{label}</button>)}</div>
            </> : <p className="py-10 text-slate-400">Waiting for telemetry. This node may have been started without --simulate.</p>}
          </>}
        </section>
      </div>
      <AiInvestigator key={chosen?.session || "empty"} node={chosen} online={online} />
      <div className="grid gap-5 lg:grid-cols-2">
        <section className={panel}><h2 className="mb-3 text-lg font-medium">Gossip event feed</h2><p className="mb-3 text-xs text-slate-400">Signed events received from the backend. Routine telemetry is shown in the chart.</p>
          <div className="max-h-80 space-y-2 overflow-auto">{events.length ? events.map((e) => <div className="rounded bg-slate-800 p-3 text-sm" key={`${e.session}:${e.seq}`}><span className="font-medium text-sky-300">{e.origin}</span> · {e.kind}<p className="mt-1 break-words text-xs text-slate-400">{JSON.stringify(e.data)}</p></div>) : <p className="text-slate-400">No alerts or rescue events yet. Try opening a fridge door.</p>}</div>
        </section>
        <section className={panel}><h2 className="mb-3 text-lg font-medium">Food rescue coordination</h2><p className="mb-3 text-xs text-slate-400">Requests and offers travel between fridges. Offers are advisory; no stock is moved automatically.</p>
          {requests.length ? requests.map((request) => <div className="mb-3 rounded bg-slate-800 p-3" key={`${request.session}:${request.seq}`}><p>{request.origin} requested {request.data.litres} L</p><p className="text-sm text-slate-400">{offers.filter((o) => o.data.request_session === request.session && o.data.request_seq === request.seq).map((o) => `${o.origin}: ${o.data.litres} L offered`).join(" · ") || "Waiting for neighbour offers"}</p></div>) : <p className="text-slate-400">No stock at risk.</p>}
          {data && <p className="mt-5 text-xs text-slate-500">Last backend snapshot: {new Date(data.timestamp * 1000).toLocaleTimeString()}</p>}
        </section>
      </div>
    </div>
  </main>;
}
