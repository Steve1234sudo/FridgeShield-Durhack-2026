/*
  NetworkFeed: a plain-English feed of what the fridges told each other.

    import NetworkFeed from "./NetworkFeed";
    <NetworkFeed feed={state?.feed} />

  `feed` comes from the backend's /ws stream. Each item reads like:
    F1 reported a possible sensor fault to F2 and F3.
    F2 and F3 stopped trusting F1's temperature readings.
    Advice: Check F1 with a handheld thermometer.
  It's built from the fridges' own delivery records and signed replies, so it's real
  evidence of peer-to-peer communication, not text made up by a central server.
*/
const TONE = {
  warning: "bg-amber-400",
  fixed: "bg-sky-400",
  good: "bg-emerald-400",
  info: "bg-slate-400",
};

const timeOf = (seconds) =>
  new Date(seconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const duration = (ms) => (ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`);

function Relay({ item }) {
  const direct = item.heard_by.filter((h) => h.direct);
  const relayed = item.heard_by.filter((h) => !h.direct);
  if (!item.heard_by.length) return null;
  const parts = [];
  if (direct.length) parts.push(`direct in ${direct.map((h) => duration(h.after_ms)).join(", ")}`);
  relayed.forEach((h) => parts.push(`${h.fridge} heard via ${h.via}`));
  return <p className="mt-0.5 text-xs text-slate-400">{parts.join(" · ")}</p>;
}

export default function NetworkFeed({ feed = [] }) {
  const items = [...(feed || [])].reverse();
  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900 p-4">
      <h2 className="font-semibold">What the fridges are telling each other</h2>
      <p className="mb-3 text-xs text-slate-400">Sent directly between neighbouring fridges, with no central server.</p>
      {items.length === 0 ? (
        <p className="text-sm text-slate-400">All quiet. Reports, decisions and advice between fridges will appear here.</p>
      ) : (
        <ul className="max-h-96 space-y-3 overflow-y-auto pr-1">
          {items.map((item) => (
            <li key={item.id} className="flex items-start gap-2 text-sm">
              <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${TONE[item.tone] || TONE.info}`} />
              <div className="min-w-0 flex-1">
                <p>
                  <span className="mr-2 tabular-nums text-xs text-slate-400">{timeOf(item.at)}</span>
                  <span className="font-medium text-slate-100">{item.text}</span>
                </p>
                {item.outcome && <p className="text-slate-300">{item.outcome}</p>}
                {item.advice && <p className="text-sky-300">{item.advice}</p>}
                <Relay item={item} />
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
