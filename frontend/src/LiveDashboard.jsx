import { useEffect, useRef, useState } from "react";
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer } from "recharts";
import { ShieldCheck, Plus, Trash2, Zap, DoorOpen, Thermometer, Unplug, Plug, Pause, Volume2, VolumeX } from "lucide-react";
import AiInvestigator from "./AiInvestigator";
import NetworkFeed from "./NetworkFeed";
import { startSoundscape, stopSoundscape, updateSoundscape } from "./soundscape";

const button = "inline-flex items-center gap-2 rounded-lg border border-slate-700 px-3 py-2 text-sm hover:bg-slate-800 disabled:opacity-40";
const panel = "rounded-xl border border-slate-800 bg-slate-900 p-5";
const temperature = (value) => Number.isFinite(value) ? `${value.toFixed(1)}°C` : "—";
// Node colour: grey cut off, blue protective mode, amber any other warning, green normal.
const needsAttention = (n) => n.isolated || (n.local && (n.local.security !== "normal" || n.local.target_overridden || n.local.door));
const nodeColours = (n) => !n.local ? { fill: "#1e293b", line: "#64748b" }
  : n.isolated ? { fill: "#334155", line: "#94a3b8" }
  : n.local.security === "protective" ? { fill: "#0c4a6e", line: "#38bdf8" }
  : needsAttention(n) ? { fill: "#78350f", line: "#fbbf24" }
  : { fill: "#064e3b", line: "#34d399" };

// A fridge drawn in SVG: freezer compartment on top, handles on the right, its temperature on
// the front, and the door swung open when it's open. Colours show its state.
function FridgeIcon({ node, x, y, selected }) {
  const W = 46, H = 70, left = x - W / 2, top = y - H / 2, split = top + 22;
  const { fill, line } = nodeColours(node);
  const dash = node.isolated ? "5 4" : undefined;
  const shown = node.local?.security === "protective" ? node.local?.estimate : node.local?.reported;
  return <>
    {selected && <rect x={left - 6} y={top - 6} width={W + 12} height={H + 12} rx="10" fill="none" stroke="#f8fafc" strokeWidth="1.5" />}
    {node.local?.door && <path d={`M ${left} ${top + 2} L ${left - 20} ${top + 9} L ${left - 20} ${top + H - 9} L ${left} ${top + H - 2} Z`}
      fill={fill} fillOpacity="0.5" stroke={line} strokeWidth="1.5" />}
    <rect x={left} y={top} width={W} height={H} rx="6" fill={fill} stroke={line} strokeWidth="2" strokeDasharray={dash} />
    <line x1={left} y1={split} x2={left + W} y2={split} stroke={line} strokeWidth="1.5" strokeDasharray={dash} />
    <rect x={left + W - 8} y={top + 6} width="3" height="10" rx="1.5" fill={line} />
    <rect x={left + W - 8} y={split + 6} width="3" height="18" rx="1.5" fill={line} />
    <text x={x - 3} y={split + 29} textAnchor="middle" fill="#f1f5f9" fontSize="11">{temperature(shown)}</text>
    <text x={x} y={top - 9} textAnchor="middle" fill="#f1f5f9" fontSize="13" fontWeight="600">{node.id}</text>
    {node.isolated && <text x={x} y={top + H + 15} textAnchor="middle" fill="#94a3b8" fontSize="10">cut off</text>}
  </>;
}
const linkKey = (a, b) => [a, b].sort().join("|");

export default function LiveDashboard() {
  const socket = useRef(null);
  const [data, setData] = useState(null);
  const [status, setStatus] = useState("connecting");
  const [selected, setSelected] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [truth, setTruth] = useState(false);
  const [soundOn, setSoundOn] = useState(false);
  // The soundscape follows the fridges: every update may retune a voice.
  useEffect(() => { updateSoundscape(Object.values(data?.nodes || {})); }, [data]);
  useEffect(() => () => stopSoundscape(), []);
  const toggleSound = async () => {
    if (soundOn) { stopSoundscape(); setSoundOn(false); return; }
    setSoundOn(true);
    try { await startSoundscape(Object.values(data?.nodes || {})); }
    catch (err) { setSoundOn(false); setError(`Couldn't start the soundscape: ${err.message}`); }
  };
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
  const radius = Math.max(110, nodes.length * 14);
  const width = radius * 2 + 180;
  const positions = Object.fromEntries(ids.map((id, i) => {
    const angle = i * 2 * Math.PI / ids.length - Math.PI / 2;
    return [id, { x: width / 2 + radius * Math.cos(angle), y: width / 2 + radius * Math.sin(angle) }];
  }));
  const online = status === "connected";
  // Messages delivered between neighbours in the last moment: light up the link and show a moving dot.
  const flows = Object.values(Object.fromEntries((data?.active_links || [])
    .filter((l) => positions[l.from] && positions[l.to]).map((l) => [`${l.from}>${l.to}`, l])));
  const busyLinks = new Set(flows.map((l) => linkKey(l.from, l.to)));
  const liveLinks = new Set((data?.links || []).map(([a, b]) => linkKey(a, b)));
  const distrustedBy = nodes.filter((n) => n.local?.distrusted_sensors?.includes(chosen?.id)).map((n) => n.id);

  return <main className="min-h-screen bg-slate-950 p-4 text-slate-100 md:p-7">
    <style>{`
      @keyframes fs-flow { from { transform: translate(0px, 0px); opacity: 1; } to { transform: translate(var(--dx), var(--dy)); opacity: .4; } }
      .fs-flow { animation: fs-flow .8s linear infinite; }
      @media (prefers-reduced-motion: reduce) { .fs-flow { animation: none; } }
    `}</style>
    <div className="mx-auto max-w-7xl space-y-5">
      <header className="flex flex-wrap items-center gap-3">
        <ShieldCheck className="text-sky-400" /><h1 className="text-2xl font-semibold">FridgeShield</h1>
        <span className={`rounded-full px-3 py-1 text-xs ${online ? "bg-emerald-950 text-emerald-300" : "bg-amber-950 text-amber-300"}`}>{status}</span>
        <span className="text-sm text-slate-400">Live backend · simulated sensors</span>
        <button className={`${button} ml-auto`} onClick={toggleSound} aria-pressed={soundOn}
          title="Each fridge plays a note in one chord. A fridge under attack goes out of tune.">
          {soundOn ? <VolumeX size={16} /> : <Volume2 size={16} />}{soundOn ? "Mute soundscape" : "Play soundscape"}</button>
        <button className={button} disabled={!online || busy} onClick={() => command("add")}><Plus size={16} />Add fridge</button>
      </header>
      {!online && <div role="status" className={`${panel} border-amber-800 text-amber-200`}>Waiting for backend. Start <code>python -m backend.live</code> from the repository root. {data && "Displayed readings are stale until reconnection."}</div>}
      {error && <div role="alert" className="rounded-lg bg-red-950 p-3 text-red-200">{error}</div>}
      {notice && <p role="status" className="text-sm text-sky-300">{notice}</p>}
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        {[ ["Active fridges", nodes.length], ["Direct peer links", data?.links.length || 0],
          ["Need attention", nodes.filter(needsAttention).length],
          ["Unreachable", data?.unreachable.length || 0] ].map(([label, value]) => <div className={panel} key={label}><p className="text-sm text-slate-400">{label}</p><p className="mt-2 text-3xl font-semibold">{value}</p></div>)}
      </div>
      <div className="grid gap-5 lg:grid-cols-2">
        <section className={panel}>
          <h2 className="text-lg font-medium">Neighbour network</h2><p className="text-sm text-slate-400">Lines show actual connected WebSocket peers. Select a fridge.</p>
          {nodes.length ? <svg className="max-h-96 w-full" viewBox={`0 0 ${width} ${width}`} role="img" aria-label="Live fridge network">
            {(data.topology || data.links).map(([a, b]) => {
              const up = liveLinks.has(linkKey(a, b)), talking = busyLinks.has(linkKey(a, b));
              return <line key={`${a}-${b}`} x1={positions[a].x} y1={positions[a].y} x2={positions[b].x} y2={positions[b].y}
                stroke={!up ? "#ef4444" : talking ? "#38bdf8" : "#334155"} strokeWidth={talking ? 4 : 3}
                strokeDasharray={up ? undefined : "6 6"} opacity={up ? 1 : 0.6} />;
            })}
            {flows.map((l) => <circle key={`${l.from}>${l.to}`} cx={positions[l.from].x} cy={positions[l.from].y} r="6" className="fs-flow"
              fill={l.kind === "ack" ? "#34d399" : "#fbbf24"}
              style={{ "--dx": `${positions[l.to].x - positions[l.from].x}px`, "--dy": `${positions[l.to].y - positions[l.from].y}px` }} />)}
            {nodes.map((node) => <g key={node.id} role="button" tabIndex={0} aria-label={`Select ${node.id}`} onClick={() => setSelected(node.id)} onKeyDown={(e) => { if (e.key === "Enter") setSelected(node.id); }} className="cursor-pointer">
              <FridgeIcon node={node} x={positions[node.id].x} y={positions[node.id].y} selected={chosen?.id === node.id} />
            </g>)}
          </svg> : <p className="py-16 text-center text-slate-400">No fridges online. Add one to start.</p>}
          <p className="mb-3 text-xs text-slate-400">Moving dots are messages travelling between neighbours right now: amber for warnings, green for replies. Red dashed lines are cut links. Fridge colours: green normal, amber warning, blue protective mode, grey cut off and in safe mode. An open door is drawn swung open.</p>
          <div className="flex flex-wrap gap-2">{ids.map((id) => <button className={button} key={id} onClick={() => setSelected(id)}>{id}</button>)}</div>
        </section>
        <section className={panel}>
          <div className="flex items-center justify-between"><h2 className="text-lg font-medium">{chosen ? `Fridge ${chosen.id}` : "Fridge details"}</h2>
            {chosen && <button className={button} disabled={!online || busy || !data.managed.includes(chosen.id)} onClick={() => command("remove", chosen.id)}><Trash2 size={16} />Remove</button>}
          </div>
          {chosen && <>
            <p className="mt-2 text-sm text-slate-400">Neighbours: {chosen.neighbours.join(", ") || "none"} · Connected: {chosen.connected.join(", ") || "none"}</p>
            <p className="text-sm text-slate-400">{Object.keys(chosen.clock).length} origin sessions · {chosen.event_count} known events · {chosen.rejected} rejected</p>
            {distrustedBy.length > 0 && <p className="mt-3 rounded-lg bg-amber-950 p-3 text-sm text-amber-200">{distrustedBy.join(" and ")} {distrustedBy.length === 1 ? "has" : "have"} stopped trusting {chosen.id}'s temperature readings.</p>}
            {local?.reported !== undefined ? <>
              {chosen.isolated && <p className="mt-3 rounded-lg bg-slate-800 p-3 text-sm text-slate-200">{chosen.id} is cut off from its neighbours and the AI, and running in safe mode on its default settings. Remote attacks can't reach it, so try one: it will be blocked. It keeps protecting itself with its own reflexes, and everything it logs syncs to its neighbours when it reconnects.</p>}
              {local.defrost && <p className="mt-3 rounded-lg bg-sky-950 p-3 text-sm text-sky-200">Defrost cycle: {chosen.id} is warming on purpose to clear ice from its coils. This is normal and isn't treated as a fault.</p>}
              {local.target_overridden && <p className="mt-3 rounded-lg bg-amber-950 p-3 text-sm text-amber-200">Target temperature overridden to {temperature(local.target)} (safe setting {temperature(local.safe_target)}), so the fridge has stopped cooling properly. The AI can restore it; otherwise the fridge restores it itself before its food passes {temperature(local.safe_target + 5)}.</p>}
              {local.spoofed && <p className="mt-3 rounded-lg bg-amber-950 p-3 text-sm text-amber-200">Sensor spoofing is active: the sensor reports a fake value near {temperature(local.target - 1)}, even while the fridge cools. {local.security === "protective" ? "Protective mode controls cooling from the independent model estimate." : "The controller is checking the sensor against its independent thermal model."} Use the estimate and simulation ground truth to inspect cooling.</p>}
              <p className="mt-3 text-xs text-slate-400">Data: {local.data_file ? `real recording (${local.data_file})` : "simulator"} · Cooling control: {local.control_source || "sensor"} · Sensor/estimate gap: {temperature(local.sensor_gap)} · Blue chart line: sensor · Pink: estimate from the {local.estimate_source || "thermal model"}</p>
              <div className="my-4 grid grid-cols-2 gap-3 text-sm">
                {[["Sensor", temperature(local.reported)], ["Estimate", temperature(local.estimate)], ["Target", local.target_overridden ? `${temperature(local.target)} (overridden)` : temperature(local.target)], ["Power", local.power_watts >= 1000 ? `${(local.power_watts / 1000).toFixed(1)} kW` : `${local.power_watts.toFixed(0)} W`], ["Security", local.security], ["Compressor", local.compressor ? "on" : "off"], ["Door", local.door ? "open" : "closed"], ["Network", chosen.isolated ? "cut off" : "connected"]].map(([k, v]) => <div className="flex justify-between gap-2 rounded bg-slate-800 p-2" key={k}><span className="text-slate-400">{k}</span><span>{v}</span></div>)}
              </div>
              <div className="h-48"><ResponsiveContainer width="100%" height="100%"><LineChart data={local.history}>
                <XAxis dataKey="t" hide /><YAxis stroke="#64748b" domain={local.chart_range || ["auto", "auto"]} allowDataOverflow={!!local.chart_range} /><Tooltip contentStyle={{ background: "#0f172a", border: "1px solid #334155" }} />
                <Line dataKey="reported" name="Sensor °C" stroke="#38bdf8" dot={false} isAnimationActive={false} />
                <Line dataKey="estimate" name="Estimate °C" stroke="#fb7185" dot={false} isAnimationActive={false} />
                {truth && <Line dataKey="actual" name="Actual °C" stroke="#d879ef" dot={false} isAnimationActive={false} />}
              </LineChart></ResponsiveContainer></div>
              <label className="text-xs text-slate-400"><input type="checkbox" checked={truth} onChange={(e) => setTruth(e.target.checked)} /> Show simulation ground truth</label>
              <div className="mt-4 flex flex-wrap gap-2">{[["spoof_sensor", local.spoofed ? "Stop spoofing" : "Spoof sensor", Zap], ["open_door", local.door ? "Close door" : "Open door", DoorOpen], ["override_target", local.target_overridden ? "Undo target override" : "Override target temperature", Thermometer], ["isolate", chosen.isolated ? "Reconnect" : "Cut off communication", chosen.isolated ? Plug : Unplug], ["pause", local.paused ? "Resume" : "Pause", Pause]].map(([name, label, Icon]) => <button className={button} disabled={!online || busy} key={name} onClick={() => command(name, chosen.id)}><Icon size={14} />{label}</button>)}</div>
            </> : <p className="py-10 text-slate-400">Waiting for telemetry. This node may have been started without --simulate.</p>}
          </>}
        </section>
      </div>
      <div className="grid gap-5 lg:grid-cols-2">
        <AiInvestigator fridgeId={chosen?.id} incidents={data?.incidents} />
        <NetworkFeed feed={data?.feed} />
      </div>
      {data && <p className="text-xs text-slate-500">Last backend snapshot: {new Date(data.timestamp * 1000).toLocaleTimeString()}</p>}
    </div>
  </main>;
}
