import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, TRACKER, type AgentEvent, type ExtensionInfo, type Settings, type TrackerRow, type TrackerTotals } from "../api";
import { clock } from "../lib/tracker";
import { heldHint, isFormChanged, labelKind, labelText, localDay, originText, routedText, totals, type LabelKind } from "../lib/tracker";
import { useAgentThread, useApp, usePoll } from "../state";
import { AiToggle } from "../components/AiToggle";
import { IncidentBanner } from "../components/IncidentBanner";
import { Card, Empty, PageHead, Spinner } from "../components/ui";

// ------------------------------------------------------------------------------- data
/** The tracker, polled every second so a hotkey press shows up within ~1 s. */
export function useTracker(ms = 1000) {
  return usePoll(() => api.tracker({ integration: TRACKER, limit: 50 }), ms);
}

/** Rows that arrived after the first load, so they can flash once. */
function useNewRows(rows: TrackerRow[] | undefined): Set<string> {
  const seen = useRef<Set<string> | null>(null);
  const [fresh, setFresh] = useState<Set<string>>(new Set());
  useEffect(() => {
    if (!rows) return;
    if (seen.current === null) {
      seen.current = new Set(rows.map((r) => r.page_id));
      return;
    }
    const add = rows.filter((r) => !seen.current!.has(r.page_id)).map((r) => r.page_id);
    if (!add.length) return;
    add.forEach((id) => seen.current!.add(id));
    setFresh((f) => new Set([...f, ...add]));
    const t = setTimeout(() => setFresh((f) => new Set([...f].filter((id) => !add.includes(id)))), 2400);
    return () => clearTimeout(t);
  }, [rows]);
  return fresh;
}

// ------------------------------------------------------------------------------- pieces
const CHIP: Record<LabelKind, { fg: string; bg: string }> = {
  code: { fg: "var(--ok)", bg: "var(--ok-soft)" },
  learned: { fg: "var(--accent)", bg: "var(--accent-soft)" },
  held: { fg: "var(--val)", bg: "var(--val-soft)" },
  duplicate: { fg: "var(--dup)", bg: "var(--dup-soft)" },
  learning: { fg: "var(--accent)", bg: "var(--accent-soft)" },
  pending: { fg: "var(--muted)", bg: "var(--sunken)" },
  failed: { fg: "var(--bad)", bg: "var(--bad-soft)" },
};
const ICON: Record<LabelKind, string> = { code: "✓", learned: "AI", held: "!", duplicate: "=", learning: "", pending: "", failed: "✗" };

export function LabelChip({ row, settings, big = false }: { row: TrackerRow; settings: Settings | null; big?: boolean }) {
  const k = labelKind(row);
  const s = k === "held" && isFormChanged(row) ? CHIP.failed : CHIP[k];
  return (
    <span className="pill" style={{ color: s.fg, background: s.bg, fontSize: big ? 17 : undefined, padding: big ? "7px 10px" : undefined, whiteSpace: "normal", lineHeight: 1.25 }}>
      {(k === "learning" || k === "pending") && <Spinner />}
      {ICON[k] && <b aria-hidden>{ICON[k]}</b>}
      {labelText(row, settings)}
    </span>
  );
}

function Stat({ label, value, sub, tone, big }: { label: string; value: ReactNode; sub?: ReactNode; tone?: string; big?: boolean }) {
  return (
    <div className="card card-b grid gap-1 content-start">
      <span className="eyebrow" style={big ? { fontSize: 15 } : undefined}>{label}</span>
      <span className={`font-semibold leading-none ${big ? "text-[64px]" : "text-[30px]"}`} style={{ color: tone ?? "var(--ink)" }}>{value}</span>
      {sub && <span className={`${big ? "text-[16px]" : "text-[12.5px]"} text-muted`}>{sub}</span>}
    </div>
  );
}

/** The counter row. AI calls are counted for pages captured today (local time). */
export function TrackerStats({ rows, settings, big = false }: { rows: TrackerRow[]; settings: Settings | null; big?: boolean }) {
  const t: TrackerTotals = totals(rows, localDay(new Date()));
  const scripted = settings?.agent === "scripted";
  return (
    <div className={`grid gap-3 grid-cols-2 ${big ? "lg:grid-cols-4" : "sm:grid-cols-3 lg:grid-cols-6"}`}>
      <Stat big={big} label="AI calls today" value={t.ai_calls} tone={t.ai_calls ? "var(--accent)" : "var(--ok)"}
        sub={t.learned_now ? `all from ${t.learned_now} new layout${t.learned_now === 1 ? "" : "s"}${scripted ? " (scripted agent: no model)" : ""}` : "known layouts are read by code"} />
      <Stat big={big} label="Written by code" value={t.code} sub="0 AI calls each" tone="var(--ok)" />
      <Stat big={big} label="Learned now" value={t.learned_now} sub="AI once, checked by code" tone="var(--accent)" />
      <Stat big={big} label="Held for a person" value={t.held} sub="nothing written" tone={t.held ? "var(--val)" : undefined} />
      {!big && <Stat label="Already in tracker" value={t.duplicate} sub="same posting, skipped" />}
      {!big && <Stat label="Pages today" value={t.pages} sub="hotkey presses" />}
    </div>
  );
}

/** Evidence the learn thread produced for this page: each value, where code found it, pass/fail. */
export function LearnEvidence({ threadId, settings }: { threadId: string; settings: Settings | null }) {
  const { events } = useAgentThread(threadId);
  const last = <T extends AgentEvent["type"]>(type: T) => [...events].reverse().find((e) => e.type === type) as Extract<AgentEvent, { type: T }> | undefined;
  const ev = last("evidence");
  const learned = last("learned");
  const done = last("done");
  const calls = (done?.result as { model_calls?: number } | undefined)?.model_calls;
  const scripted = settings?.agent === "scripted";
  if (!events.length) return <div className="text-muted text-sm">Loading the learn thread…</div>;
  return (
    <div className="grid gap-2">
      <div className="text-[13px]">
        <b>{scripted ? "Proposed by the scripted agent (no model ran)" : `Proposed by AI${calls != null ? ` in ${calls} call${calls === 1 ? "" : "s"}` : ""}`}</b>
        {" · every value checked by code against the page"} <span className="font-mono text-xs text-muted">({threadId})</span>
      </div>
      {ev ? (
        <table className="tbl">
          <thead>
            <tr><th>Field</th><th>Value</th><th>Found in</th><th>Reader</th><th>Check</th></tr>
          </thead>
          <tbody>
            {ev.fields.map((f) => (
              <tr key={f.name}>
                <td className="font-mono text-[12.5px]">{f.name}</td>
                <td className="font-semibold">{f.value ?? <span className="text-muted">—</span>}</td>
                <td className="text-[12.5px]">{f.where === "jsonld" ? "embedded JSON-LD" : f.where === "html" ? "page HTML (attribute)" : f.where === "text" ? "visible page text" : "not found"}</td>
                <td className="font-mono text-[12px] text-muted break-all">{typeof f.extractor === "string" ? f.extractor : JSON.stringify(f.extractor)}</td>
                <td className="font-semibold" style={{ color: f.ok ? "var(--ok)" : "var(--bad)" }}>{f.ok ? "✓ found" : "✗ not found"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div className="text-muted text-sm">No evidence yet: the learner is still reading the page.</div>
      )}
      {learned && (
        <div className="text-[13px]">
          <b>{learned.approval === "auto" ? "Auto-approved" : learned.approval === "human" ? "Approved by a person" : "Waiting for a person"}</b>
          {learned.version != null && <> · v{learned.version}</>} · {learned.reason}
        </div>
      )}
    </div>
  );
}

function RowDetail({ row, settings, onReplay }: { row: TrackerRow; settings: Settings | null; onReplay: () => void }) {
  const { go, setThreadId } = useApp();
  const hint = heldHint(row, settings);
  const o = row.parser_origin ? originText(row.parser_origin) : null;
  return (
    <div className="grid gap-3 px-4 py-3 bg-sunken">
      <div className="flex flex-wrap gap-x-6 gap-y-1 text-[13px]">
        <span><span className="text-muted">Page</span> <span className="font-mono">{row.page_id}</span></span>
        <a className="break-all" href={row.url} target="_blank" rel="noreferrer">{row.url}</a>
        {row.run_id != null && <span><span className="text-muted">run</span> #{row.run_id}</span>}
        {o && <span><span className="text-muted">reader</span> {o.how} · {o.approval}</span>}
      </div>
      {hint && (
        <div className="rounded-md border-l-4 px-3 py-2 grid gap-2" style={{ borderColor: isFormChanged(row) ? "var(--bad)" : "var(--val)", background: "var(--surface)" }}>
          <div className="text-[14px]"><b>Why it's held:</b> {row.detail ?? row.reason}</div>
          <div className="text-[14px]"><b>What you can do:</b> {hint}</div>
          <div className="flex flex-wrap gap-2 items-center">
            {row.reason === "PARSE_NO_MATCH" && !settings?.ai_enabled && <AiToggle />}
            {row.reason === "PARSE_NO_MATCH" && !row.learn_thread_id && (
              <button className="btn btn-primary" onClick={onReplay} disabled={!settings?.ai_enabled} title={settings?.ai_enabled ? "Replay held pages on the active version" : "Turn AI on first"}>
                Replay held pages
              </button>
            )}
            {row.learn_thread_id && (
              <button className="btn" onClick={() => { setThreadId(row.learn_thread_id); go("agent"); }}>Open the learn thread →</button>
            )}
          </div>
        </div>
      )}
      {!hint && row.detail && <div className="text-[13px] text-muted">{row.detail}</div>}
      {row.learn_thread_id && <LearnEvidence threadId={row.learn_thread_id} settings={settings} />}
    </div>
  );
}

function Posting({ row, big }: { row: TrackerRow; big?: boolean }) {
  const extracted = row.status === "success" || row.status === "duplicate";
  return (
    <div className="grid gap-0.5 min-w-0">
      <span className={`font-semibold ${big ? "text-[26px] leading-tight" : "text-[14.5px]"}`} style={extracted ? undefined : { color: "var(--muted)" }}>
        {row.title ?? "(no title)"}
        {!extracted && row.title && <span className="font-normal text-[12px]"> (page title, not read yet)</span>}
      </span>
      <span className={big ? "text-[19px]" : "text-[13px]"}>
        {row.company ?? <span className="text-muted">—</span>} <span className="font-mono text-muted" style={{ fontSize: big ? 15 : 12 }}>· {row.site}</span>
      </span>
    </div>
  );
}

/** The live tracker table. `big` is the presenter version (fewer columns, large type, no expanding). */
export function TrackerTable({ rows, settings, big = false, onReplay }: { rows: TrackerRow[]; settings: Settings | null; big?: boolean; onReplay?: () => void }) {
  const fresh = useNewRows(rows);
  const [open, setOpen] = useState<string | null>(null);
  if (!rows.length) {
    return (
      <Empty>
        No pages yet. Press <kbd className="font-mono">Alt+Shift+P</kbd> on a job posting with the Patchwork extension (setup below), and the row
        appears here within a second or two.
      </Empty>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="tbl" style={big ? { fontSize: 18 } : undefined}>
        <thead>
          <tr>
            <th className="w-[78px]">Time</th>
            <th>Posting</th>
            {!big && <th>Read by</th>}
            <th>Result</th>
            <th className="text-right">AI calls</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const isOpen = open === r.page_id;
            return (
              <Fragment key={r.page_id}>
                <tr
                  className={fresh.has(r.page_id) ? "row-new" : undefined}
                  style={{ cursor: big ? undefined : "pointer" }}
                  onClick={big ? undefined : () => setOpen(isOpen ? null : r.page_id)}
                  aria-expanded={big ? undefined : isOpen}
                >
                  <td className="font-mono text-muted num" style={{ fontSize: big ? 16 : 12.5 }}>{clock(r.captured_at)}</td>
                  <td><Posting row={r} big={big} /></td>
                  {!big && (
                    <td className="text-[13px]">
                      {r.parser ? (
                        <>
                          <span className="font-mono font-semibold">{r.parser}</span> <span className="font-mono text-muted">v{r.version}</span>
                          <div className="text-[12px] text-muted">{routedText(r)}{r.parser_origin?.kind === "learned" && r.parser_origin.at ? ` · learned ${clock(r.parser_origin.at)}` : ""}</div>
                        </>
                      ) : (
                        <span className="text-muted">no reader yet</span>
                      )}
                    </td>
                  )}
                  <td><LabelChip row={r} settings={settings} big={big} /></td>
                  <td className="text-right num font-semibold" style={{ fontSize: big ? 26 : 15, color: r.ai_calls ? "var(--accent)" : "var(--muted)" }}>
                    {r.ai_calls}
                    {!big && <span className="ml-2 text-muted font-normal text-xs" aria-hidden>{isOpen ? "▾" : "▸"}</span>}
                  </td>
                </tr>
                {isOpen && (
                  <tr>
                    <td colSpan={5} className="p-0">
                      <RowDetail row={r} settings={settings} onReplay={onReplay ?? (() => {})} />
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ------------------------------------------------------------------------------- extension setup
function CopyField({ label, value, secret = false }: { label: string; value: string; secret?: boolean }) {
  const [shown, setShown] = useState(!secret);
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = value;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };
  return (
    <div className="grid gap-1">
      <span className="label m-0">{label}</span>
      <div className="flex gap-2 items-center min-w-0">
        <code className="input font-mono text-[13px] truncate" style={{ padding: "6px 9px" }}>{shown ? value : "•".repeat(Math.min(24, value.length))}</code>
        {secret && <button className="btn" onClick={() => setShown((s) => !s)}>{shown ? "Hide" : "Show"}</button>}
        <button className="btn" onClick={copy} aria-label={`Copy ${label}`}>{copied ? "Copied ✓" : "Copy"}</button>
      </div>
    </div>
  );
}

export function ExtensionCard() {
  const [ext, setExt] = useState<ExtensionInfo | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api.extension().then(setExt, (e) => setErr(e instanceof Error ? e.message : String(e)));
  }, []);
  return (
    <Card eyebrow="Hotkey" title="Extension setup">
      <div className="grid gap-3">
        <ol className="m-0 pl-5 text-[13.5px] grid gap-1">
          <li>Chrome → <span className="font-mono">chrome://extensions</span> → Developer mode → <b>Load unpacked</b> → the repo's <span className="font-mono">extension/</span> folder.</li>
          <li>Open the extension's options and paste the two values below.</li>
          <li>On any job posting press <kbd className="font-mono font-semibold">Alt+Shift+P</kbd>. The badge shows <b>✓</b> code, <b>AI</b> learned now, <b>!</b> held.</li>
        </ol>
        {ext ? (
          <>
            <CopyField label="Control URL" value={ext.control_url} />
            <CopyField label="Token" value={ext.token} secret />
          </>
        ) : (
          <div className="text-muted text-sm">{err ? `Couldn't load the setup values: ${err}` : "Loading…"}</div>
        )}
      </div>
    </Card>
  );
}

// ------------------------------------------------------------------------------- tab
export default function TrackerTab() {
  const { settings, incidents, act, refresh } = useApp();
  const q = useTracker(1000);
  const rows = q.data?.rows ?? [];
  const held = rows.filter((r) => r.status === "held").length;
  const incident = useMemo(() => incidents.find((i) => i.integration === TRACKER && i.status === "open"), [incidents]);
  const replay = async () => {
    const r = await act(() => api.replay(TRACKER));
    if (r) {
      refresh();
      void q.reload();
    }
  };
  return (
    <>
      <PageHead
        eyebrow="Job postings → tracker form"
        title="Tracker"
        right={
          <>
            {q.error && !q.data && <span className="pill" style={{ color: "var(--bad)", background: "var(--bad-soft)" }}>tracker unavailable</span>}
            <a className="btn no-underline" href="?present=1" title="Big type for the room; → and ← step through the 2-minute script">Presenter mode</a>
            <button className="btn" onClick={replay} disabled={!held} title={held ? "Run held pages again on the active version" : "Nothing is held"}>
              Replay held pages{held ? ` (${held})` : ""}
            </button>
          </>
        }
      >
        Every hotkey press on a job posting and what happened to it. Layouts Patchwork already knows are read by code with
        0 AI calls. A new layout is learned once with AI, every value checked by code, and labelled. Anything in doubt is
        held for a person and nothing is written.
      </PageHead>

      {incident && <IncidentBanner key={incident.id} id={incident.id} />}

      <TrackerStats rows={rows} settings={settings} />

      <Card
        eyebrow="Live · updates every second"
        title="Pages sent with the hotkey"
        right={<span className="font-mono text-xs text-muted">click a row for details and evidence</span>}
        bodyClass=""
      >
        {q.data ? <TrackerTable rows={rows} settings={settings} onReplay={replay} /> : <div className="card-b text-muted">{q.error ? `Couldn't load the tracker: ${q.error}` : "Loading…"}</div>}
      </Card>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_380px] items-start">
        <div id="layouts-slot" />
        <ExtensionCard />
      </div>
    </>
  );
}
