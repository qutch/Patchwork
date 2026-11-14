// Presenter mode (?present=1): the 2-minute demo (design §2) for the back of the room.
// Big type, the live tracker (polled every second), the AI-calls counter, and the current beat of the
// script with what to say and do. → / Space next beat, ← back, 1–6 jump, T restarts the clock, Esc leaves.
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { api, type BenchResults, type Layout, type TrackerRow } from "../api";
import { aiCallSeries, aiSummary, caughtTally } from "../lib/bench";
import { isJobs, JOBS_WORDING, sourceText, type SinkField, type Wording } from "../lib/integrations";
import { clock, fieldColumns, fieldLabel, layoutRows, localDay, originText, totals } from "../lib/tracker";
import { useApp, usePoll } from "../state";
import { TrackerTable, usePageView, useTracker } from "../tabs/Tracker";
import { CredentialWarning, DestinationBadge, IntegrationPicker, useShowIntegration } from "./Destination";
import { useBenchV2 } from "../tabs/Benchmark";
import { AiCallsChart, AiLegend } from "./AiCallsChart";
import { AiToggle } from "./AiToggle";
import { GateCard } from "./gates";
import { IncidentBanner } from "./IncidentBanner";

export interface Beat {
  at: number; // seconds into the demo when this beat starts
  title: string;
  say: string;
  do: string;
  view: "hook" | "teach" | "tracker" | "break" | "proof" | "close";
}
export const BEATS: Beat[] = [
  { at: 0, view: "hook", title: "The boring part",
    say: "I track every job I apply to. Copying title, company and link is the boring part. An AI agent could do it, but it reads every page with AI forever. A Claude script breaks the first time a site looks different.",
    do: "Tracker form and Sheet visible beside this window." },
  { at: 15, view: "teach", title: "Show it once",
    say: "I showed Patchwork once: copy title, company and link from one posting into my tracker form. It wrote the integration and its tests.",
    do: "Replay the teaching recording (or do it live); point at the readers below." },
  { at: 35, view: "tracker", title: "Regardless of layout",
    say: "Now the hotkey on three different sites. Known layouts: written by code, zero AI calls. This one is new: Patchwork learns it once, code checks every value, labelled 'learned now'. The next one from that site: zero.",
    do: "Alt+Shift+P on Lever, Greenhouse, then the new platform twice. Watch the counter." },
  { at: 65, view: "break", title: "Break it, live",
    say: "Rename a question in the form: Company becomes Employer. Next press is blocked before any write. The AI proposes the remap; I approve; the held posting goes in.",
    do: "Judge renames the question → hotkey → Repair with AI → approve the yellow card." },
  { at: 95, view: "proof", title: "Proof",
    say: "AI calls per posting over real postings: a browser agent climbs forever; Patchwork flattens to zero once it knows the layouts. And in v1 a one-shot Claude script silently lost records; Patchwork caught both breaks.",
    do: "Let the chart sit for a beat." },
  { at: 115, view: "close", title: "Code that learns",
    say: "Code that learns. AI is the teacher, not the worker.",
    do: "Stop." },
];

const fmtClock = (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

function useBeat() {
  const [i, setI] = useState(() => {
    const b = Number(new URLSearchParams(location.search).get("beat"));
    return Number.isInteger(b) && b >= 1 && b <= BEATS.length ? b - 1 : 0;
  });
  const [t0, setT0] = useState<number | null>(null);
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(id);
  }, []);
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement | null)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (e.target as HTMLElement | null)?.getAttribute?.("role") === "img") return;
      if (e.key === " " && tag === "BUTTON") return; // Space on a focused button presses it
      if (e.key === "ArrowRight" || e.key === " " || e.key === "PageDown") {
        setI((x) => Math.min(BEATS.length - 1, x + 1));
        setT0((t) => t ?? Date.now());
      } else if (e.key === "ArrowLeft" || e.key === "PageUp") setI((x) => Math.max(0, x - 1));
      else if (/^[1-6]$/.test(e.key)) setI(Number(e.key) - 1);
      else if (e.key.toLowerCase() === "t") setT0(Date.now());
      else if (e.key === "Escape") location.href = location.pathname + "#tracker";
      else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, []);
  const elapsed = t0 == null ? 0 : (now - t0) / 1000;
  return { i, beat: BEATS[i], elapsed, started: t0 != null };
}

function Big({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`display font-extrabold leading-[1.05] tracking-tight ${className}`}>{children}</div>;
}

/** The room's counter: AI calls today, huge; the label counts beside or under it. */
function Counter({ rows, scripted, wide = false, w = JOBS_WORDING }: { rows: TrackerRow[]; scripted: boolean; wide?: boolean; w?: Wording }) {
  const t = totals(rows, localDay(new Date()));
  const line = (label: string, n: number, color?: string) => (
    <div className="flex items-baseline justify-between gap-3 text-[20px]">
      <span className="text-muted">{label}</span>
      <b className="text-[30px] leading-none" style={{ color }}>{n}</b>
    </div>
  );
  return (
    <div className={`card card-b grid gap-3 ${wide ? "grid-cols-[auto_minmax(0,1fr)] items-center gap-x-10" : ""}`}>
      <div className="grid gap-1">
        <span className="eyebrow" style={{ fontSize: 15 }}>AI calls today</span>
        <span className="font-semibold leading-none" style={{ fontSize: wide ? 120 : 96, color: t.ai_calls ? "var(--accent)" : "var(--ok)" }}>{t.ai_calls}</span>
        <span className="text-[15px] text-muted">{t.learned_now ? `only for ${t.learned_now} new layout${t.learned_now === 1 ? "" : "s"}${scripted ? " · scripted agent, no model" : ""}` : "known layouts: read by code"}</span>
      </div>
      <div className={`grid gap-2 ${wide ? "grid-cols-2 gap-x-10" : ""}`}>
        {line("written by code", t.code, "var(--ok)")}
        {line("learned now", t.learned_now, "var(--accent)")}
        {line("held for a person", t.held, t.held ? "var(--val)" : undefined)}
        {line(w.duplicate, t.duplicate)}
      </div>
    </div>
  );
}

const JOBS_TEACH: [string, string][] = [["Job Title", "title read from the page"], ["Company", "company read from the page"], ["Link", "the page's canonical URL"], ["Date Applied", "the day you pressed the hotkey"]];

/** What each destination field is filled from: the jobs demo's four, else the active spec's sink fields (or the field names). */
export function teachPairs(name: string, sinkFields: SinkField[], fieldNames: string[]): [string, string][] {
  if (isJobs(name)) return JOBS_TEACH;
  const fromSink = sinkFields.filter((f) => !(f.source && typeof f.source === "object" && "const" in f.source));
  if (fromSink.length) return fromSink.map((f) => [f.label, sourceText(f.source)]);
  return fieldNames.map((f) => [fieldLabel(f), `${f.replace(/_/g, " ")} read from the page`]);
}

function TeachView({ layouts, pairs }: { layouts: Layout[]; pairs: [string, string][] }) {
  return (
    <div className="grid gap-6">
      <Big className="text-[52px]">Shown once. Every field has a reader.</Big>
      <div className="grid grid-cols-2 gap-4 text-[24px]">
        {pairs.map(([k, v]) => (
          <div key={k} className="card card-b"><b>{k}</b> <span className="text-muted">← {v}</span></div>
        ))}
      </div>
      <div className="grid gap-2">
        <div className="eyebrow" style={{ fontSize: 15 }}>Layouts it knows</div>
        <div className="flex flex-wrap gap-3">
          {layoutRows(layouts).filter((l) => l.active).map((l) => (
            <span key={l.parser_id} className="card card-b text-[20px]">
              <b>{l.site ?? l.parser_id}</b> <span className="text-muted text-[16px]">· {originText(l.origin).how}{l.origin?.at ? ` ${clock(l.origin.at)}` : ""}</span>
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}

function ProofView({ v1 }: { v1: BenchResults | null }) {
  const { data, error } = useBenchV2();
  const series = useMemo(() => (data ? aiCallSeries(data) : []), [data]);
  const sum = data ? aiSummary(data) : null;
  const tally = v1 ? caughtTally(v1) : null;
  return (
    <div className="grid gap-5">
      {data && sum ? (
        <>
          <div className="flex flex-wrap items-baseline gap-x-8 gap-y-2">
            <Big className="text-[34px]">{sum.postings} real postings: Patchwork {sum.patchwork} AI calls{sum.browser != null ? `, browser agent ≈ ${sum.browser}` : ""}</Big>
            {data.mock && <span className="pill text-[16px]" style={{ color: "var(--bad)", background: "var(--bad-soft)" }}>mock data</span>}
          </div>
          <AiLegend series={series} big />
          <AiCallsChart series={series} height={340} big label={`After the last new layout: ${sum.zeroAfter} postings, 0 AI calls`} />
        </>
      ) : (
        <Big className="text-[40px] text-muted">{error?.status === 404 ? "Bench v2 hasn't run yet (bench/v2/results.json)." : "Loading the benchmark…"}</Big>
      )}
      {tally && (
        <div className="card card-b text-[24px]">
          v1 benchmark: breaks caught before any bad write: <b>Patchwork {tally.patchwork}/{tally.total}</b> · one-shot Claude script {tally.baseline_script}/{tally.total}
        </div>
      )}
    </div>
  );
}

export default function Presenter() {
  const { settings, incidents, threads, pageIntegration: name } = useApp();
  const { i, beat, elapsed, started } = useBeat();
  const view = usePageView(name);
  const { w } = view;
  const showIntegration = useShowIntegration(name, view.destination);
  const tracker = useTracker(name, 1000);
  const columns = fieldColumns(tracker.data);
  const layouts = usePoll(async () => ({ name, layouts: await api.layouts(name) }), 3000, [name]);
  const layoutList = layouts.data?.name === name ? layouts.data.layouts : [];
  const [v1, setV1] = useState<BenchResults | null>(null);
  useEffect(() => {
    api.benchResults().then(setV1, () => setV1(null));
  }, []);
  const rows = tracker.data?.rows ?? [];
  const incident = incidents.find((x) => x.integration === name && x.status === "open");
  const gate = [...threads].reverse().find((t) => t.state === "waiting" && t.pending && (t.mode === "learn" || t.mode === "repair"));
  const next = BEATS[i + 1];
  const end = next?.at ?? 120;
  const late = started && elapsed > end + 3;
  const scripted = settings?.agent === "scripted";

  return (
    <div className="min-h-screen grid grid-rows-[auto_auto_1fr]" style={{ fontSize: 20 }}>
      <header className="flex items-center gap-5 px-6 py-2.5 border-b border-line bg-surface">
        <span className="display text-[26px] font-extrabold">Patchwork</span>
        <ol className="flex gap-1.5 m-0 p-0 list-none" aria-label="Beats">
          {BEATS.map((b, k) => (
            <li key={b.title} className="font-mono text-[14px] px-2.5 py-1 rounded whitespace-nowrap" title={b.title}
              style={k === i ? { background: "var(--accent)", color: "var(--surface)" } : { color: k < i ? "var(--muted)" : "var(--ink)", background: "var(--sunken)" }}>
              {k + 1}{k === i ? ` ${b.title}` : ""}
            </li>
          ))}
        </ol>
        <div className="ml-auto flex items-center gap-4 whitespace-nowrap">
          <IntegrationPicker big compact />
          {scripted && <span className="pill text-[14px]" style={{ color: "var(--muted)", background: "var(--sunken)" }} title="No model runs: the scripted agent stands in">scripted agent</span>}
          <span className="font-mono text-[22px] num" style={{ color: late ? "var(--bad)" : "var(--ink)" }} title="Starts on the first →; T restarts it">
            {started ? fmtClock(elapsed) : "0:00"} <span className="text-muted text-[15px]">/ {fmtClock(beat.at)}–{fmtClock(end)}</span>
          </span>
          <AiToggle />
        </div>
      </header>

      <section className="px-6 py-3 grid gap-1 border-b border-line" style={{ background: "var(--accent-soft)" }} aria-live="polite">
        <div className="flex items-baseline gap-4 flex-wrap">
          <span className="display text-[26px] font-extrabold">{i + 1}. {beat.title}</span>
          <span className="text-[16px] text-muted">Do: {beat.do}</span>
          {next && <span className="ml-auto font-mono text-[14px] text-muted">→ next: {next.title}</span>}
        </div>
        <div className="text-[19px] leading-snug max-w-[130ch]">“{beat.say}”</div>
      </section>

      <main className="px-6 py-5 grid gap-5 content-start">
        {beat.view === "hook" && (
          <div className="grid gap-8 py-4">
            <Big className="text-[68px]">Show it once.<br />Fill it in regardless of layout.</Big>
            <Counter rows={rows} scripted={scripted} wide w={w} />
          </div>
        )}
        {beat.view === "teach" && <TeachView layouts={layoutList} pairs={teachPairs(name, view.sinkFields, columns)} />}
        {(beat.view === "tracker" || beat.view === "break") && (
          <div className="grid gap-5 grid-cols-[minmax(0,1fr)_260px] items-start">
            <div className="grid gap-4 min-w-0">
              {/* a waiting gate (repair or learn approval) is the next thing to do, so it replaces the banner */}
              {showIntegration && (
                <div className="flex flex-wrap items-center gap-3">
                  <span className="eyebrow" style={{ fontSize: 15 }}>{view.eyebrow}</span>
                  <DestinationBadge name={name} d={view.destination} big />
                </div>
              )}
              <CredentialWarning name={name} d={view.destination} big />
              {gate?.pending ? <GateCard key={gate.pending.interrupt_id} ev={gate.pending} threadId={gate.thread_id} />
                : incident && <IncidentBanner key={incident.id} id={incident.id} big onStarted={() => {}} />}
              <div className="card">
                <TrackerTable rows={rows.slice(0, incident || gate ? 3 : 6)} settings={settings} big columns={columns} w={w} />
              </div>
            </div>
            <Counter rows={rows} scripted={scripted} w={w} />
          </div>
        )}
        {beat.view === "proof" && <ProofView v1={v1} />}
        {beat.view === "close" && (
          <div className="grid gap-8 py-8">
            <Big className="text-[80px]">Code that learns.<br />AI is the teacher, not the worker.</Big>
            <Counter rows={rows} scripted={scripted} wide w={w} />
          </div>
        )}
      </main>
    </div>
  );
}
