/*
  AiInvestigator: the AI panel for the FridgeShield dashboard.

  Put it under the selected fridge's details:

    import AiInvestigator from "./AiInvestigator";
    <AiInvestigator fridgeId={chosen?.id} incidents={data?.incidents} />

  - `fridgeId` is the selected fridge, e.g. "F4".
  - `incidents` comes from the latest message on the backend's /ws stream, which now
    includes automatic incident explanations.
  - Questions go to POST /api/ask, which asks that fridge for its own view first.

  The AI explains problems and can apply a few safe fixes (close the door, protective
  mode, restore the target temperature), which the fridge checks and carries out itself. If the
  backend or Gemini is down, the fridges carry on and their own reflexes still apply.
*/
import { useEffect, useRef, useState } from "react";
import { Bot, Send } from "lucide-react";
import IncidentReport from "./IncidentReport";

// Same address as the dashboard: Vite's proxy forwards /api and /health to the backend.
const DEFAULT_API = (import.meta.env.VITE_BACKEND_HTTP || "").replace(/\/$/, "");
const LIKELIHOOD_TONE = { high: "text-red-300", medium: "text-amber-300", low: "text-slate-400" };
const clock = (seconds) => new Date(seconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

const ACTION_LABEL = {
  close_door: "Closed the door",
  protective_mode: "Switched to protective mode",
  restore_target: "Restored the safe target temperature",
};

const ACTION_PLAN = {
  close_door: "Close the door",
  protective_mode: "Switch to protective mode",
  restore_target: "Restore the safe target temperature",
};

function ActionsTaken({ actions }) {
  if (!actions?.length) return null;
  return (
    <ul className="mt-2 space-y-1">
      {actions.map((a, i) => {
        // A pending fix counts down (AI_FIX_DELAY on the backend); the stream refreshes it.
        const seconds = a.pending ? Math.max(0, Math.ceil(a.applies_at - Date.now() / 1000)) : 0;
        const tone = a.applied ? "border-emerald-700 text-emerald-300"
          : a.pending ? "border-sky-700 text-sky-300" : "border-amber-700 text-amber-300";
        return (
          <li key={i} className={`rounded-lg border px-3 py-1.5 text-xs ${tone}`}>
            <span className="font-medium">
              {a.pending ? `Fixing in ${seconds}s: ${ACTION_PLAN[a.command] || a.command}`
                : `${a.applied ? "Done: " : "Not applied: "}${ACTION_LABEL[a.command] || a.command}`}.
            </span>{" "}
            <span className="text-slate-400">{a.applied || a.pending ? a.reason : a.message}</span>
          </li>
        );
      })}
    </ul>
  );
}

function List({ title, items }) {
  if (!items?.length) return null;
  return (
    <div className="mt-2">
      <p className="mb-1 text-xs text-slate-400">{title}</p>
      <ul className="list-disc space-y-0.5 pl-5">{items.map((x, i) => <li key={i}>{x}</li>)}</ul>
    </div>
  );
}

function Answer({ result }) {
  const d = result.diagnosis;
  return (
    <div className="text-sm leading-relaxed text-slate-200">
      {d ? (
        <>
          <p>{d.summary}</p>
          <ActionsTaken actions={result.actions_taken} />
          {d.hypotheses?.length > 0 && (
            <div className="mt-2">
              <p className="mb-1 text-xs text-slate-400">Possible causes</p>
              <ul className="space-y-1.5">
                {d.hypotheses.map((h, i) => (
                  <li key={i} className="rounded-lg bg-slate-800 px-3 py-2">
                    <div className="flex justify-between gap-3">
                      <span>{h.cause}</span>
                      <span className={`shrink-0 ${LIKELIHOOD_TONE[h.likelihood] || "text-slate-400"}`}>{h.likelihood}</span>
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <List title="What to do" items={d.recommended_actions} />
          <List title="Still unknown" items={d.unknowns} />
        </>
      ) : (
        <p className="text-red-300">{result.error || "No answer came back."}</p>
      )}
      {result.invalid_citations?.length > 0 && (
        <p className="mt-2 text-xs text-amber-300">The AI referred to evidence that doesn't exist, so double-check this answer.</p>
      )}
      {result.latency_ms > 0 && (
        <p className="mt-1 text-xs text-slate-400">{result.model} answered in {(result.latency_ms / 1000).toFixed(1)} s</p>
      )}
    </div>
  );
}

function IncidentCard({ incident }) {
  const resolved = incident.resolved_at != null;
  return (
    <div className={`mb-3 rounded-lg border px-3 py-2.5 ${resolved ? "border-slate-700" : "border-amber-500"}`}>
      <div className="mb-1.5 flex items-center justify-between gap-2 text-xs">
        <span className={resolved ? "text-slate-400" : "font-medium text-amber-300"}>
          Automatic alert at {clock(incident.created_at)}: {incident.concern}
        </span>
        {resolved && <span className="text-emerald-400">Resolved at {clock(incident.resolved_at)}</span>}
      </div>
      <Answer result={incident} />
    </div>
  );
}

export default function AiInvestigator({ fridgeId, incidents = [], apiUrl = DEFAULT_API }) {
  const [threads, setThreads] = useState({}); // one conversation per fridge
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState(false);
  const [online, setOnline] = useState(null); // null = checking, false = offline, string = model name
  const listRef = useRef(null); // the scrollable chat box

  useEffect(() => {
    fetch(`${apiUrl}/health`).then((r) => r.json()).then((h) => setOnline(h.model)).catch(() => setOnline(false));
  }, [apiUrl]);

  const thread = threads[fridgeId] || [];
  const incident = [...(incidents || [])].reverse().find((i) => i.fridge_id === fridgeId);
  // Keep the newest message in view by scrolling the chat box only. (scrollIntoView would also
  // scroll the whole page, which jumped the dashboard down when switching between fridges.)
  useEffect(() => {
    const box = listRef.current;
    if (box) box.scrollTop = box.scrollHeight;
  }, [thread.length, pending, fridgeId]);

  const add = (id, message) => setThreads((t) => ({ ...t, [id]: [...(t[id] || []), message] }));

  const ask = async (question) => {
    if (!question.trim() || pending || !fridgeId) return;
    const id = fridgeId;
    setDraft("");
    add(id, { role: "user", text: question });
    setPending(true);
    try {
      const res = await fetch(`${apiUrl}/api/ask`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ fridge_id: id, question }),
      });
      if (!res.ok) throw new Error(`status ${res.status}`);
      const result = await res.json();
      setOnline(result.model || true);
      add(id, { role: "ai", result });
    } catch (err) {
      setOnline(false);
      add(id, { role: "ai", result: { error: `Couldn't reach the AI (${err.message}). The fridges keep running without it.` } });
    } finally {
      setPending(false);
    }
  };

  const presets = [`What's wrong with ${fridgeId}?`, `Can we trust ${fridgeId}'s sensor?`, "What should staff do now?"];
  const status = online === null ? "Checking for the AI…"
    : online ? `Connected, using ${online}`
    : "AI offline. The fridges keep protecting themselves without it.";

  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900 p-4">
      <div className="mb-1 flex items-center gap-2">
        <Bot className="h-5 w-5 text-sky-400" />
        <h2 className="font-semibold">AI investigator{fridgeId ? `: ${fridgeId}` : ""}</h2>
        <div className="ml-auto"><IncidentReport fridgeId={fridgeId} apiUrl={apiUrl} /></div>
      </div>
      <p className={`mb-3 text-xs ${online ? "text-emerald-400" : "text-slate-400"}`}>{status}</p>

      {!fridgeId ? (
        <p className="text-sm text-slate-400">Select a fridge to ask about it.</p>
      ) : (
        <>
          {incident && <IncidentCard incident={incident} />}

          <div ref={listRef} className="max-h-96 space-y-3 overflow-y-auto pr-1">
            {thread.length === 0 && !incident && (
              <p className="text-sm text-slate-400">Ask about {fridgeId}. The AI sees what {fridgeId} itself knows.</p>
            )}
            {thread.map((m, i) => m.role === "user" ? (
              <div key={i} className="ml-auto max-w-xs rounded-lg bg-slate-800 px-3 py-2 text-sm">{m.text}</div>
            ) : (
              <div key={i}>
                {m.result.fridge_id && m.result.fridge_id !== fridgeId && (
                  <p className="mb-1 text-xs text-slate-400">Answered from {m.result.fridge_id}'s point of view</p>
                )}
                <Answer result={m.result} />
              </div>
            ))}
            {pending && <p className="animate-pulse text-sm text-slate-400">Asking {fridgeId} what it knows…</p>}
          </div>

          <div className="mt-3 flex flex-wrap gap-1.5">
            {presets.map((p) => (
              <button key={p} onClick={() => ask(p)}
                className="rounded-full border border-slate-700 px-2.5 py-1 text-xs text-slate-300 hover:bg-slate-800 focus:outline-none focus:ring-2 focus:ring-sky-400">
                {p}
              </button>
            ))}
          </div>
          <div className="mt-2 flex gap-2">
            <input value={draft} onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") ask(draft); }}
              placeholder={`Ask about ${fridgeId}`}
              className="flex-1 rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm text-slate-100 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-sky-400" />
            <button onClick={() => ask(draft)} aria-label="Send question"
              className="inline-flex items-center rounded-lg border border-slate-700 px-3 py-1.5 text-slate-200 hover:bg-slate-800 focus:outline-none focus:ring-2 focus:ring-sky-400">
              <Send className="h-4 w-4" />
            </button>
          </div>
        </>
      )}
    </section>
  );
}
