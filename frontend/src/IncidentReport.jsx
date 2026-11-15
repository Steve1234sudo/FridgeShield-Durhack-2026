/*
  IncidentReport: a "Report" button for the selected fridge.

  Asks the backend (POST /api/report) for an incident report: exact facts gathered from the
  fridges' own records (timeline, temperatures, who fixed what) plus a short write-up by the AI.
  Shows it in a pop-up with a Download button that saves it as a Markdown (.md) file.
  If the AI is unavailable, the report still has every fact, just no written summary.
*/
import { useEffect, useRef, useState } from "react";
import { Download, FileText, X } from "lucide-react";

const clock = (seconds) => (seconds ? new Date(seconds * 1000).toLocaleTimeString() : "?");

function Section({ title, children }) {
  return (
    <section className="mt-5">
      <h3 className="mb-2 font-semibold text-slate-100">{title}</h3>
      {children}
    </section>
  );
}

export function ReportView({ report }) {
  const { facts, writing } = report;
  const t = facts.temperature;
  return (
    <div className="text-sm leading-relaxed text-slate-300">
      <p className="text-xs text-slate-400">
        Generated {clock(report.generated_at)} · Data: {facts.data_source}
      </p>
      {writing && <Section title="Summary"><p>{writing.summary}</p></Section>}
      <Section title="Timeline">
        {facts.timeline.length ? (
          <table className="w-full text-left">
            <tbody>
              {facts.timeline.map((e, i) => (
                <tr key={i} className="border-t border-slate-800 align-top">
                  <td className="w-24 py-1.5 pr-3 tabular-nums text-slate-400">{clock(e.at)}</td>
                  <td className="py-1.5">{e.what}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <p>No incidents recorded for this fridge in this session.</p>}
      </Section>
      {t.peak_c != null && (
        <Section title="Temperature">
          <ul className="list-disc space-y-0.5 pl-5">
            <li>Safe target {facts.now.safe_target_c}°C, limit {t.limit_c}°C</li>
            <li>Peak {t.peak_c}°C at {clock(t.peak_at)}</li>
            <li>Time above the limit: {t.seconds_above_limit} seconds (not counting planned defrosts)</li>
            <li>Now: sensor {facts.now.sensor_c}°C, independent estimate {facts.now.estimate_c}°C</li>
          </ul>
        </Section>
      )}
      {writing ? (
        <>
          <Section title="Likely cause"><p>{writing.likely_cause}</p></Section>
          <Section title="Impact"><p>{writing.impact}</p></Section>
          {writing.recommendations?.length > 0 && (
            <Section title="Recommendations">
              <ul className="list-disc space-y-0.5 pl-5">{writing.recommendations.map((r, i) => <li key={i}>{r}</li>)}</ul>
            </Section>
          )}
          {writing.open_questions?.length > 0 && (
            <Section title="Open questions">
              <ul className="list-disc space-y-0.5 pl-5">{writing.open_questions.map((q, i) => <li key={i}>{q}</li>)}</ul>
            </Section>
          )}
        </>
      ) : (
        <p className="mt-5 text-amber-300">
          The AI summary wasn't available{report.error ? ` (${report.error})` : ""}, so this report contains the recorded facts only.
        </p>
      )}
    </div>
  );
}

export default function IncidentReport({ fridgeId, apiUrl = "" }) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [report, setReport] = useState(null);
  const [error, setError] = useState("");
  const [reportFridge, setReportFridge] = useState(null);
  const dialogRef = useRef(null);
  const closeRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const previousFocus = document.activeElement;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeRef.current?.focus();
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); setOpen(false); }
      if (e.key === "Tab") {
        const controls = [...(dialogRef.current?.querySelectorAll('button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]') || [])];
        const first = controls[0], last = controls[controls.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus(); }
        if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = previousOverflow;
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  }, [open]);

  const generate = async () => {
    if (loading) { setOpen(true); return; }
    setReportFridge(fridgeId);
    setOpen(true);
    setLoading(true);
    setReport(null);
    setError("");
    try {
      const res = await fetch(`${apiUrl}/api/report`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ fridge_id: fridgeId }),
        signal: AbortSignal.timeout(60000),
      });
      if (!res.ok) throw new Error(`the backend replied with status ${res.status}`);
      const data = await res.json();
      if (!data.facts) throw new Error(data.error || "no report came back");
      setReport(data);
    } catch (err) {
      setError(err.name === "TimeoutError" ? "The report took too long. Close this window and try again."
        : `Couldn't create the report: ${err.message}. Check the backend connection and try again.`);
    } finally {
      setLoading(false);
    }
  };

  const download = () => {
    const blob = new Blob([report.markdown], { type: "text/markdown" });
    const link = document.createElement("a");
    const stamp = new Date(report.generated_at * 1000).toISOString().slice(0, 16).replace(/[:T]/g, "-");
    link.href = URL.createObjectURL(blob);
    link.download = `FridgeShield-report-${report.fridge_id}-${stamp}.md`;
    link.click();
    URL.revokeObjectURL(link.href);
  };

  const button = "inline-flex min-h-10 items-center justify-center gap-1.5 rounded-lg border border-slate-700 px-2.5 py-1 text-xs text-slate-200 hover:bg-slate-800 focus:outline-none focus:ring-2 focus:ring-sky-400 disabled:cursor-not-allowed disabled:opacity-40";

  return (
    <>
      <button className={button} onClick={generate} disabled={!fridgeId} aria-busy={loading} title="Create an incident report for this fridge">
        <FileText className="h-3.5 w-3.5" />Report
      </button>
      {open && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" onClick={() => setOpen(false)}>
          <div ref={dialogRef} role="dialog" aria-modal="true" aria-label={`Incident report for ${reportFridge}`} aria-busy={loading}
            className="max-h-[85vh] w-full max-w-2xl overflow-y-auto break-words rounded-xl border border-slate-700 bg-slate-900 p-4 sm:p-6"
            onClick={(e) => e.stopPropagation()}>
            <div className="flex flex-wrap items-center gap-3">
              <h2 className="min-w-0 break-words text-lg font-semibold">Incident report: {reportFridge}</h2>
              <div className="ml-auto flex gap-2">
                {report && <button className={button} onClick={download}><Download className="h-3.5 w-3.5" />Download</button>}
                <button ref={closeRef} className={button} onClick={() => setOpen(false)} aria-label="Close report"><X className="h-3.5 w-3.5" /></button>
              </div>
            </div>
            <div className="mt-3">
              {loading && <p role="status" className="text-sm text-slate-400">Gathering {reportFridge}'s records and writing the report…</p>}
              {error && <p role="alert" className="text-sm text-red-300">{error}</p>}
              {report && <ReportView report={report} />}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
