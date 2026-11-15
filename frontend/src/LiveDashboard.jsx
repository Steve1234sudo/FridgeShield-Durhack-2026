import { useEffect, useRef, useState } from "react";
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer } from "recharts";
import { ShieldCheck, Plus, Trash2, Zap, DoorOpen, Thermometer, Unplug, Plug, Pause, Volume2, VolumeX } from "lucide-react";
import AiInvestigator from "./AiInvestigator";
import NetworkFeed from "./NetworkFeed";
import { startSoundscape, stopSoundscape, updateSoundscape } from "./soundscape";
import { arrangeFridges } from "./networkLayout";

const button = "inline-flex min-h-10 items-center justify-center gap-2 rounded-lg border border-slate-700 px-3 py-2 text-sm hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40";
const panel = "min-w-0 rounded-xl bg-slate-900 p-4 sm:p-5";
const temperature = (value) => Number.isFinite(value) ? `${value.toFixed(1)}°C` : "—";

// One colour per kind of problem, shared by the fridge on the map and the button that causes it.
const KINDS = {
  normal: { label: "Normal", fill: "#064e3b", line: "#34d399" },
  sensor: { label: "Sensor spoofed", fill: "#2e1065", line: "#a78bfa",
    button: "border-violet-500/70 text-violet-200 hover:bg-violet-950", on: "bg-violet-900/70" },
  target: { label: "Target overridden", fill: "#4c0519", line: "#fb7185",
    button: "border-rose-500/70 text-rose-200 hover:bg-rose-950", on: "bg-rose-900/70" },
  door: { label: "Door open", fill: "#451a03", line: "#fbbf24",
    button: "border-amber-500/70 text-amber-200 hover:bg-amber-950", on: "bg-amber-900/70" },
  cutoff: { label: "Cut off", fill: "#334155", line: "#94a3b8",
    button: "border-slate-400/70 text-slate-200 hover:bg-slate-800", on: "bg-slate-700" },
};
// What's wrong with a fridge, as far as the network knows. A spoofed sensor shows once it's detected.
const problem = (n) => !n.local ? "normal" : n.isolated ? "cutoff" : n.local.target_overridden ? "target"
  : n.local.security !== "normal" ? "sensor" : n.local.door ? "door" : "normal";

// A fridge drawn in SVG: freezer compartment on top, handles on the right, its temperature on
// the front, and the door swung open when it's open. Its colour shows what's wrong with it.
function FridgeIcon({ node, x, y, selected }) {
  const W = 46, H = 70, left = x - W / 2, top = y - H / 2, split = top + 22;
  const { fill, line } = KINDS[problem(node)];
  const dash = node.isolated ? "5 4" : undefined;
  const shown = node.local?.security === "protective" ? node.local?.estimate : node.local?.reported;
  const note = node.isolated ? "cut off" : node.local?.security === "protective" ? "protecting"
    : problem(node) !== "normal" ? KINDS[problem(node)].label : null;
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
    {note && <text x={x} y={top + H + 15} textAnchor="middle" fill={line} fontSize="10">{note}</text>}
  </>;
}
// Evenly spaced temperature labels for the chart, e.g. 0°, 4°, 8°, 12°, 16° for a -2 to 16°C range.
const chartTicks = (range) => {
  if (!range) return undefined;
  const [lo, hi] = range, step = Math.max(1, Math.ceil((hi - lo) / 4 / 2) * 2);
  const ticks = [];
  for (let t = Math.ceil(lo / step) * step; t <= hi; t += step) ticks.push(t);
  return ticks;
};
const linkKey = (a, b) => [a, b].sort().join("|");

export default function LiveDashboard() {
  const socket = useRef(null);
  const commandTimer = useRef(null);
  const [data, setData] = useState(null);
  const [status, setStatus] = useState("connecting");
  const [selected, setSelected] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [truth, setTruth] = useState(false);
  const [soundOn, setSoundOn] = useState(false);
  // Every server start has a new ID. When it changes, start from a clean slate: the AI chat
  // (remounted via its key below), the selected fridge and any messages are all cleared.
  const serverId = data?.server_id;
  const lastServer = useRef(null);
  useEffect(() => {
    if (!serverId) return;
    if (lastServer.current && lastServer.current !== serverId) {
      setSelected(null);
      setError("");
      setNotice("New server session: logs and AI chat cleared.");
    }
    lastServer.current = serverId;
  }, [serverId]);
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
      active.onopen = () => { setStatus("syncing"); setError(""); };
      active.onmessage = ({ data: raw }) => {
        const msg = JSON.parse(raw);
        if (msg.type === "state") { setData(msg); setStatus("connected"); }
        if (msg.type === "error") { clearTimeout(commandTimer.current); setError(msg.message); setBusy(false); }
        if (msg.type === "result") {
          clearTimeout(commandTimer.current);
          setBusy(false);
          setNotice(msg.result.reason || (msg.result.id ? `Added ${msg.result.id}` : "Command delivered to backend"));
        }
      };
      active.onclose = () => {
        clearTimeout(commandTimer.current);
        if (stopped) return;
        setStatus("disconnected"); setBusy(false);
        retry = setTimeout(open, 1500);
      };
      active.onerror = () => active.close();
    };
    open();
    return () => { stopped = true; clearTimeout(retry); clearTimeout(commandTimer.current); active?.close(); };
  }, []);

  const command = (name, id) => {
    if (socket.current?.readyState !== WebSocket.OPEN) return;
    setError(""); setNotice(""); setBusy(true);
    clearTimeout(commandTimer.current);
    commandTimer.current = setTimeout(() => {
      setBusy(false);
      setError("No confirmation arrived from the backend. Check the live fridge state before retrying.");
    }, 20000);
    socket.current.send(JSON.stringify({ command: name, id, requestId: crypto.randomUUID() }));
  };
  const nodes = Object.values(data?.nodes || {});
  const chosen = data?.nodes[selected] || nodes[0];
  const local = chosen?.local;
  const ids = nodes.map((n) => n.id);
  const membership = JSON.stringify([...ids].sort());
  const [layout, setLayout] = useState(() => ({ ...arrangeFridges([]), membership: "[]", serverId }));
  // Adjust only on membership changes, before React commits the new graph. This avoids
  // a frame with missing coordinates and keeps existing fridges anchored when adding one.
  let graph = layout;
  if (layout.membership !== membership || layout.serverId !== serverId) {
    graph = { ...arrangeFridges(ids, layout.serverId === serverId ? layout : undefined), membership, serverId };
    setLayout(graph);
  }
  const { positions, width, height } = graph;
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
        <span className="text-sm text-slate-400">{nodes.length} {nodes.length === 1 ? "fridge" : "fridges"} · {nodes.filter((n) => problem(n) !== "normal" || n.local?.health === "failing").length} need attention</span>
        <button className="ml-auto inline-flex min-h-10 items-center justify-center gap-2 rounded-lg bg-sky-400 px-3 py-2 text-sm font-medium text-slate-950 hover:bg-sky-300 disabled:cursor-not-allowed disabled:opacity-40" disabled={!online || busy} onClick={() => command("add")}><Plus size={16} />{busy ? "Working…" : "Add fridge"}</button>
      </header>
      {!online && <div role="status" className={`${panel} border-amber-800 text-amber-200`}>Waiting for backend. Start <code>python -m backend.live</code> from the repository root. {data && "Displayed readings are stale until reconnection."}</div>}
      {error && <div role="alert" className="break-words rounded-lg bg-red-950 p-3 text-red-200">{error}</div>}
      {notice && <p role="status" className="break-words text-sm text-sky-300">{notice}</p>}
      <div className="grid gap-5 lg:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)]">
        <section className={panel}>
          <h2 className="text-lg font-medium">Store network</h2>
          <p className="text-sm text-slate-400">Click a fridge to inspect it.</p>
          {nodes.length ? <svg className="h-80 w-full sm:h-96" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Live fridge network">
            {(data.topology || data.links || []).filter(([a, b]) => positions[a] && positions[b]).map(([a, b]) => {
              const up = liveLinks.has(linkKey(a, b)), talking = busyLinks.has(linkKey(a, b));
              return <line key={`${a}-${b}`} x1={positions[a].x} y1={positions[a].y} x2={positions[b].x} y2={positions[b].y}
                stroke={!up ? "#ef4444" : talking ? "#38bdf8" : "#334155"} strokeWidth={talking ? 4 : 3}
                strokeDasharray={up ? undefined : "6 6"} opacity={up ? 1 : 0.6} />;
            })}
            {flows.map((l) => <circle key={`${l.from}>${l.to}`} cx={positions[l.from].x} cy={positions[l.from].y} r="6" className="fs-flow"
              fill={l.kind === "ack" ? "#34d399" : "#fbbf24"}
              style={{ "--dx": `${positions[l.to].x - positions[l.from].x}px`, "--dy": `${positions[l.to].y - positions[l.from].y}px` }} />)}
            {nodes.map((node) => <g key={node.id} role="button" tabIndex={0} aria-pressed={chosen?.id === node.id} aria-label={`Select ${node.id}`} onClick={() => setSelected(node.id)} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setSelected(node.id); } }} className="cursor-pointer">
              <FridgeIcon node={node} x={positions[node.id].x} y={positions[node.id].y} selected={chosen?.id === node.id} />
            </g>)}
          </svg> : <p className="py-16 text-center text-slate-400">No fridges online. Add one to start.</p>}
          <details className="mt-3 text-xs text-slate-400"><summary className="cursor-pointer">Map legend</summary>
          <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-300" aria-label="Legend">
            {Object.entries(KINDS).map(([key, k]) => <li key={key} className="flex items-center gap-1.5">
              <span className="inline-block h-3 w-3 rounded-sm border" style={{ background: k.fill, borderColor: k.line }} />{k.label}</li>)}
            <li className="flex items-center gap-1.5"><span className="inline-block h-2 w-2 rounded-full bg-amber-400" />Message between fridges</li>
          </ul></details>
        </section>
        <section className={panel}>
          <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="min-w-0 break-words text-lg font-medium">{chosen ? `Fridge ${chosen.id}` : "Fridge details"}</h2>
            {chosen && <select aria-label="Select fridge" value={chosen.id} onChange={(e) => setSelected(e.target.value)} className="min-h-10 max-w-36 rounded-md border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm">{nodes.map((n) => <option key={n.id} value={n.id}>{n.id}</option>)}</select>}
          </div>
          {chosen && <>
            
            {distrustedBy.length > 0 && <p className="mt-3 rounded-lg bg-violet-950 p-3 text-sm text-violet-200">{distrustedBy.join(" and ")} {distrustedBy.length === 1 ? "has" : "have"} stopped trusting {chosen.id}'s temperature readings.</p>}
            {local?.reported !== undefined ? <>
              {chosen.isolated && <p className="mt-3 rounded-lg bg-slate-800 p-3 text-sm text-slate-200">Cut off from its neighbours and the AI, and running in safe mode: remote attacks are blocked. It keeps protecting itself, and everything it logs syncs when it reconnects.</p>}
              {local.defrost && <p className="mt-3 rounded-lg bg-sky-950 p-3 text-sm text-sky-200">Defrost cycle: warming on purpose to clear ice. This is normal, not a fault.</p>}
              {local.target_overridden && <p className="mt-3 rounded-lg bg-rose-950 p-3 text-sm text-rose-200">Target overridden to {temperature(local.target)} (safe setting {temperature(local.safe_target)}), so it has stopped cooling. The AI will restore it; if it can't, the fridge restores it itself before passing {temperature(local.safe_target + 5)}.</p>}
              {local.spoofed && <p className="mt-3 rounded-lg bg-violet-950 p-3 text-sm text-violet-200">Spoofing is on: the sensor is stuck near {temperature(local.target - 1)}. {local.security === "protective" ? "The fridge has caught it and is cooling from its independent estimate." : "Watch the estimate pull away from the sensor."}</p>}
              <div className="mt-4 grid grid-cols-2 gap-4 tabular-nums">
                <div><p className="text-sm text-slate-400">Sensor</p><p className="text-3xl font-semibold tabular-nums">{temperature(local.reported)}</p></div>
                <div><p className="text-sm text-slate-400">Independent estimate</p><p className="text-3xl font-semibold tabular-nums">{temperature(local.estimate)}</p></div>
              </div>
              <dl className="mt-4 grid grid-cols-2 gap-x-6 text-sm">
                {[["Target", local.target_overridden ? `${temperature(local.target)} (overridden)` : temperature(local.target)],
                  ["Security", local.security], ["Compressor", local.compressor ? "on" : "off"], ["Door", local.door ? "open" : "closed"]].map(([k, v]) =>
                  <div className="flex flex-wrap justify-between gap-2 border-b border-slate-800 py-1.5" key={k}><dt className="text-slate-400">{k}</dt><dd className="break-words">{v}</dd></div>)}
              </dl>
              <div className="mt-4 h-44 min-w-0"><ResponsiveContainer width="100%" height="100%" minWidth={0}><LineChart data={local.history} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
                <XAxis dataKey="t" hide /><YAxis stroke="#94a3b8" width={48} tick={{ fontSize: 11 }} domain={local.chart_range || ["auto", "auto"]} allowDataOverflow={!!local.chart_range}
                  ticks={chartTicks(local.chart_range)} tickFormatter={(v) => `${v}°`} />
                <Tooltip contentStyle={{ background: "#0f172a", border: "1px solid #334155" }} labelFormatter={() => ""}
                  formatter={(v) => `${Number(v).toFixed(1)}°C`} />
                <Line dataKey="reported" name="Sensor" stroke="#38bdf8" dot={false} isAnimationActive={false} />
                <Line dataKey="estimate" name="Estimate" stroke="#fb7185" dot={false} isAnimationActive={false} />
                {truth && <Line dataKey="actual" name="Real" stroke="#d879ef" dot={false} isAnimationActive={false} />}
              </LineChart></ResponsiveContainer></div>
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-300">
                <span className="flex items-center gap-1.5"><span className="inline-block h-0.5 w-4 bg-sky-400" />Sensor</span>
                <span className="flex items-center gap-1.5"><span className="inline-block h-0.5 w-4 bg-rose-400" />Estimate</span>
                {truth && <span className="flex items-center gap-1.5"><span className="inline-block h-0.5 w-4 bg-fuchsia-400" />Real temperature</span>}
              </div>
              <details className="mt-4 border-t border-slate-800 pt-3 text-sm"><summary className="cursor-pointer text-slate-300">Monitoring details</summary>
                <dl className="mt-3 space-y-2 text-sm"><div className="flex justify-between gap-4"><dt className="text-slate-400">Power</dt><dd>{Math.round(local.power_watts)} W</dd></div><div className="flex justify-between gap-4"><dt className="text-slate-400">Neighbours</dt><dd>{chosen.isolated ? "cut off" : chosen.neighbours.join(", ") || "none"}</dd></div></dl>
                <label className="mt-3 flex items-center gap-2 text-xs text-slate-300"><input type="checkbox" checked={truth} onChange={(e) => setTruth(e.target.checked)} />Show real temperature (simulation only)</label>
                <button className={`${button} mt-3 text-xs`} onClick={toggleSound} aria-pressed={soundOn} title="Each fridge plays a note in one chord. A fridge under attack goes out of tune.">{soundOn ? <VolumeX size={14} /> : <Volume2 size={14} />}{soundOn ? "Mute soundscape" : "Play soundscape"}</button>
              </details>
              <details className="mt-3 border-t border-slate-800 pt-3 text-sm"><summary className="cursor-pointer text-slate-300">Simulation controls</summary>
              <div className="mt-3 flex flex-wrap gap-2">{[
                ["spoof_sensor", local.spoofed ? "Stop spoofing" : "Spoof sensor", Zap, "sensor", local.spoofed],
                ["override_target", local.target_overridden ? "Undo target override" : "Override target", Thermometer, "target", local.target_overridden],
                ["open_door", local.door ? "Close door" : "Open door", DoorOpen, "door", local.door],
                ["isolate", chosen.isolated ? "Reconnect" : "Cut off", chosen.isolated ? Plug : Unplug, "cutoff", chosen.isolated],
                ["pause", local.paused ? "Resume" : "Pause", Pause, null, local.paused],
              ].map(([name, label, Icon, kind, on]) => <button key={name} disabled={!online || busy} onClick={() => command(name, chosen.id)} aria-pressed={!!on}
                className={kind ? `${button} ${KINDS[kind].button} ${on ? KINDS[kind].on : ""}` : button}><Icon size={14} />{label}</button>)}</div>
                <button className={`${button} mt-3 text-xs text-slate-400`} disabled={!online || busy || !data.managed.includes(chosen.id)} onClick={() => command("remove", chosen.id)}><Trash2 size={14} />Remove fridge</button>
              </details>
            </> : <p className="py-10 text-slate-400">Waiting for telemetry. This node may have been started without --simulate.</p>}
          </>}
        </section>
      </div>
      <div className="grid gap-5 lg:grid-cols-2">
        <AiInvestigator key={serverId || "waiting"} fridgeId={chosen?.id} incidents={data?.incidents} />
        <NetworkFeed feed={data?.feed} />
      </div>
    </div>
  </main>;
}
