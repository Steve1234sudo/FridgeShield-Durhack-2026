import { useEffect, useRef, useState } from "react";
import { Bot, Send } from "lucide-react";
import { investigationPacket } from "./investigationPacket";

export default function AiInvestigator({ node, online }) {
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [answer, setAnswer] = useState(null);
  const [evidence, setEvidence] = useState([]);
  const request = useRef(null);
  useEffect(() => () => request.current?.abort(), []);

  const ask = async (text) => {
    if (!node?.local || !online || busy || !text.trim()) return;
    const controller = new AbortController();
    request.current = controller;
    const timeout = setTimeout(() => controller.abort(), 60000);
    const packet = investigationPacket(node);
    setBusy(true); setError(""); setAnswer(null); setEvidence(packet.events);
    try {
      const base = (import.meta.env.VITE_BACKEND_HTTP || "").replace(/\/$/, "");
      const response = await fetch(`${base}/investigate`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: text.trim(), packet }), signal: controller.signal,
      });
      if (!response.ok) throw new Error(response.status === 404
        ? "Investigator endpoint unavailable. Restart the combined backend with python -m backend.main."
        : `Investigation failed (HTTP ${response.status}). Check the backend terminal.`);
      const result = await response.json();
      if (result.error) throw new Error(result.error);
      if (!result.diagnosis) throw new Error("The AI returned no diagnosis. Please try again.");
      setAnswer({ ...result, snapshotTick: node.local.t, asked: text.trim() });
    } catch (err) {
      if (!controller.signal.aborted) setError(err.message || "Could not reach the investigator backend.");
      else setError("Investigation timed out or was cancelled. Please try again.");
    } finally {
      clearTimeout(timeout);
      if (!controller.signal.aborted) setBusy(false);
      else if (request.current === controller) setBusy(false);
    }
  };
  const disabled = busy || !online || !node?.local;
  const diagnosis = answer?.diagnosis;
  return <section className="rounded-xl border border-slate-800 bg-slate-900 p-5" aria-label="AI investigator">
    <h2 className="flex items-center gap-2 text-lg font-medium"><Bot className="text-sky-400" size={21} />AI investigator{node ? ` · ${node.id}` : ""}</h2>
    <p className="mt-2 text-sm text-slate-400">Ask Gemini about this fridge’s readings and the events it knows. Advice only; AI cannot change fridge settings.</p>
    {!node && <p className="mt-3 text-sm text-amber-200">Add a fridge to investigate.</p>}
    <div className="my-3 flex flex-wrap gap-2">
      {["What is happening with this fridge?", "Is there evidence of sensor spoofing?", "What should staff do next?"].map((prompt) =>
        <button key={prompt} disabled={disabled} onClick={() => { setQuestion(prompt); ask(prompt); }} className="rounded-lg border border-slate-700 px-3 py-2 text-xs hover:bg-slate-800 disabled:opacity-40">{prompt}</button>)}
    </div>
    <form className="flex gap-2" onSubmit={(event) => { event.preventDefault(); ask(question); }}>
      <input aria-label="Question for AI investigator" value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="Ask about this fridge…" maxLength={2000} disabled={busy}
        className="min-w-0 flex-1 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-sky-400" />
      <button disabled={disabled || !question.trim()} type="submit" className="inline-flex items-center gap-2 rounded-lg bg-sky-400 px-3 py-2 text-sm font-medium text-slate-950 disabled:opacity-40"><Send size={15} />{busy ? "Investigating…" : "Ask AI"}</button>
    </form>
    <div aria-live="polite" className="mt-3">
      {error && <p role="alert" className="rounded-lg bg-red-950 p-3 text-sm text-red-200">{error}</p>}
      {diagnosis && <div className="space-y-3 rounded-lg bg-slate-800 p-4 text-sm">
        <p className="text-xs text-slate-400">{answer.model} · {(answer.latency_ms / 1000).toFixed(1)}s · Snapshot at tick {answer.snapshotTick}</p>
        <p className="font-medium text-sky-200">{answer.asked}</p><p>{diagnosis.summary}</p>
        {!!answer.invalid_citations?.length && <p className="text-amber-200">The AI cited unknown events ({answer.invalid_citations.join(", ")}). Verify its explanation before acting.</p>}
        {diagnosis.hypotheses?.map((hypothesis, i) => <div key={i}><p>{hypothesis.cause} <span className="text-slate-400">({hypothesis.likelihood} likelihood)</span></p>
          <p className="text-xs text-slate-400">Supporting events: {hypothesis.supporting_events?.join(", ") || "none"} · Contradicting events: {hypothesis.contradicting_events?.join(", ") || "none"}</p></div>)}
        {!!diagnosis.unknowns?.length && <div><h3 className="font-medium">Still unknown</h3><ul className="mt-1 list-inside list-disc text-slate-300">{diagnosis.unknowns.map((item, i) => <li key={i}>{item}</li>)}</ul></div>}
        {!!diagnosis.recommended_actions?.length && <div><h3 className="font-medium">Suggested actions</h3><ul className="mt-1 list-inside list-disc text-slate-300">{diagnosis.recommended_actions.map((item, i) => <li key={i}>{item}</li>)}</ul></div>}
        <details><summary className="cursor-pointer text-xs text-slate-400">Evidence used ({evidence.length} events)</summary><ul className="mt-2 space-y-1 text-xs text-slate-400">{evidence.map((event) => <li key={event.id}>#{event.id} · {event.origin} · {event.kind}: {JSON.stringify(event.data)}</li>)}</ul></details>
      </div>}
    </div>
  </section>;
}
