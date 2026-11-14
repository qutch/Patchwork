import { useEffect, useMemo, useRef, useState } from "react";
import {
  api, finishedSummary, splitCounts, streams,
  type Counts, type Incident, type RecordRow, type RunDetail, type RunProgress, type RunSummary,
} from "../api";
import { fmtDuration, fmtTime } from "../lib/derive";
import { useApp, usePoll } from "../state";
import { AiToggle } from "../components/AiToggle";
import { Card, Empty, JsonInline, PageHead, Pill, outcomeStyle } from "../components/ui";

const REASON_ORDER = ["VALIDATION_FAILED", "NOT_FOUND", "PARSE_NO_MATCH", "SINK_CONTRACT", "AUTH", "VERIFY_MISMATCH", "UNKNOWN"];

/** Follows one run: GET for the record table, SSE for live counters while it runs. */
function useRunLive(runId: number | null, onFinished: () => void) {
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [progress, setProgress] = useState<RunProgress | null>(null);
  const [summary, setSummary] = useState<RunSummary | null>(null);
  const [t0, setT0] = useState<number | null>(null);
  const [now, setNow] = useState(Date.now());
  const finishedRef = useRef(onFinished);
  finishedRef.current = onFinished;

  useEffect(() => {
    setDetail(null);
    setProgress(null);
    setSummary(null);
    setT0(null);
    if (runId == null) return;
    let handle: { close(): void } | null = null;
    let cancelled = false;
    const load = () => api.run(runId).then((d) => !cancelled && setDetail(d), () => {});
    void api.run(runId).then(
      (d) => {
        if (cancelled) return;
        setDetail(d);
        if (d.run.status !== "running") return;
        setT0(Date.now());
        handle = streams.run(runId, {
          onMessage: (m) => {
            if (m.event === "progress") setProgress(m.data as RunProgress);
            else {
              setSummary(finishedSummary(m.data as { summary: RunSummary }));
              void load();
              finishedRef.current();
            }
          },
          isTerminal: (m) => m.event === "finished",
        });
      },
      () => {},
    );
    return () => {
      cancelled = true;
      handle?.close();
    };
  }, [runId]);

  const running = !!detail && detail.run.status === "running" && !summary;
  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => setNow(Date.now()), 100);
    return () => clearInterval(id);
  }, [running]);

  const counts: Counts = summary?.counts ?? (running ? progress?.counts : detail?.run.counts) ?? {};
  const duration = summary?.duration_s ?? (running ? (t0 ? (now - t0) / 1000 : null) : detail?.run.duration_s ?? null);
  const status = summary?.status ?? detail?.run.status ?? null;
  return { detail, progress, counts, duration, status, running };
}

function Counter({ label, value, k, big = false, sub }: { label: string; value: number; k: string; big?: boolean; sub?: string }) {
  const s = outcomeStyle(k);
  return (
    <div className="rounded-md border px-4 py-3 grid gap-1 min-w-[120px]" style={{ borderColor: s.fg, background: "var(--surface)" }}>
      <span className="font-mono text-[12px] font-semibold uppercase tracking-wide" style={{ color: s.fg }}>{label}</span>
      <b className={`display num leading-none ${big ? "text-[44px]" : "text-[30px]"}`} style={{ color: s.fg }}>{value}</b>
      {sub && <span className="font-mono text-[11px] text-muted">{sub}</span>}
    </div>
  );
}

function IncidentBanner({ id }: { id: number }) {
  const { settings, act, setThreadId, go, refresh } = useApp();
  const [inc, setInc] = useState<Incident | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api.incident(id).then(setInc, () => setInc(null));
  }, [id]);
  if (!inc) return null;
  const errors = inc.detail?.contract_errors ?? [];
  const ai = !!settings?.ai_enabled;
  const repair = async () => {
    setBusy(true);
    const r = await act(() => api.repair(inc.id));
    setBusy(false);
    if (r) {
      setThreadId(r.thread_id);
      refresh();
      go("agent");
    }
  };
  return (
    <section className="rounded-md border-2 overflow-hidden" style={{ borderColor: "var(--bad)", background: "var(--bad-soft)" }} role="alert">
      <div className="px-5 py-4 flex flex-wrap items-center gap-4 justify-between">
        <div className="grid gap-1 min-w-0">
          <div className="font-mono text-[12px] font-bold uppercase tracking-wider" style={{ color: "var(--bad)" }}>
            Incident #{inc.id} · {inc.type} · {inc.reason_codes.join(", ")} · v{inc.version}
          </div>
          <div className="display text-[22px] font-extrabold leading-tight" style={{ color: "var(--ink)" }}>
            {inc.type === "CONTRACT_CHANGED" ? "Run blocked before any write: Stockroom's contract changed" : inc.summary}
          </div>
          <div className="text-[15px]">{inc.summary}</div>
        </div>
        <div className="flex items-center gap-3 flex-wrap">
          {!ai && <AiToggle />}
          <button className="btn btn-lg btn-primary" onClick={repair} disabled={busy || !ai} title={ai ? "Start the REPAIR agent" : "Turn AI on to repair"}>
            Repair with AI →
          </button>
        </div>
      </div>
      {errors.length > 0 && (
        <div className="px-5 pb-4">
          <button className="btn" onClick={() => setOpen((o) => !o)}>
            {open ? "Hide" : "Show"} {errors.length} contract errors
          </button>
          {open && (
            <pre className="code rounded mt-2" style={{ maxHeight: 260 }}>
              {errors.join("\n")}
            </pre>
          )}
        </div>
      )}
    </section>
  );
}

function RecordGrid({ records }: { records: RecordRow[] }) {
  return (
    <div className="grid gap-[3px]" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(14px, 1fr))" }} aria-label="One cell per record, coloured by outcome">
      {records.map((r) => {
        const s = outcomeStyle(r.reason ?? r.status);
        return <span key={r.record_id} className="aspect-square rounded-[3px]" style={{ background: s.fg }} title={`${r.record_id}: ${r.reason ?? r.status}`} />;
      })}
    </div>
  );
}

function RecordsTable({ records }: { records: RecordRow[] }) {
  const [filter, setFilter] = useState<string>("all");
  const counts = useMemo(() => {
    const c: Record<string, number> = { all: records.length };
    for (const r of records) c[r.status] = (c[r.status] ?? 0) + 1;
    return c;
  }, [records]);
  const rows = records.filter((r) => filter === "all" || r.status === filter);
  return (
    <div className="grid gap-3">
      <div className="flex flex-wrap gap-1.5">
        {["all", "success", "duplicate", "quarantined"].map((k) => (
          <button key={k} className="tab-btn" aria-pressed={filter === k} onClick={() => setFilter(k)}>
            {k} <span className="num">{counts[k] ?? 0}</span>
          </button>
        ))}
      </div>
      <div className="max-h-[520px] overflow-auto border border-line rounded-md">
        <table className="tbl">
          <thead>
            <tr>
              <th>Record</th>
              <th>Sender</th>
              <th>Parser</th>
              <th>Outcome</th>
              <th>Reason code</th>
              <th>Key</th>
              <th>Sent to Stockroom / detail</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.record_id}>
                <td className="font-mono text-[13px] whitespace-nowrap">{r.record_id}</td>
                <td className="font-mono text-[12.5px]">{r.route_value ?? "—"}</td>
                <td className="font-mono text-[12.5px]">{r.parser ?? "—"}</td>
                <td><Pill k={r.status} /></td>
                <td>{r.reason ? <Pill k={r.reason} /> : <span className="text-muted">—</span>}</td>
                <td className="font-mono text-[12.5px] whitespace-nowrap">{r.dedupe_key ?? "—"}</td>
                <td className="text-[13px] max-w-[420px]">{r.sink_body ? <JsonInline value={r.sink_body} /> : <span className="text-muted">{r.detail ?? "—"}</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && <div className="p-4 text-muted">No records.</div>}
      </div>
    </div>
  );
}

function AccuracyFigure({ name, bump }: { name: string; bump: number }) {
  const q = usePoll(() => api.accuracy(name), 0, [name, bump]);
  const a = q.data;
  if (!a || !a.records) return <p className="m-0 text-muted">Run a live batch to score it against the seed's ground truth.</p>;
  const pct = (a.accuracy ?? 0) * 100;
  const wrong = a.records - a.correct;
  return (
    <div className="grid gap-3">
      <div className="flex items-baseline gap-4 flex-wrap">
        <b className="display text-[52px] font-extrabold leading-none num" style={{ color: wrong ? "var(--val)" : "var(--ok)" }}>
          {pct.toFixed(pct === 100 ? 0 : 1)}%
        </b>
        <span className="text-[15px]">
          <b className="num">{a.correct}</b> of <b className="num">{a.records}</b> records match ground truth exactly
          <span className="block text-muted text-sm">outcome, reason code and the exact body sent</span>
        </span>
      </div>
      <div className="flex h-5 rounded overflow-hidden border border-line" role="img" aria-label={`${a.correct} correct, ${wrong} wrong`}>
        <span style={{ width: `${(a.correct / a.records) * 100}%`, background: "var(--ok)" }} />
        <span style={{ width: `${(wrong / a.records) * 100}%`, background: "var(--bad)" }} />
      </div>
      {wrong > 0 && (
        <details>
          <summary className="cursor-pointer text-sm">{wrong} mismatch{wrong === 1 ? "" : "es"}</summary>
          <pre className="code rounded mt-2" style={{ maxHeight: 200 }}>{a.mismatches.map((m) => JSON.stringify(m)).join("\n")}</pre>
        </details>
      )}
    </div>
  );
}

function DemoControls({ onChange }: { onChange: () => void }) {
  const { act, notify, refresh, setRunId, setThreadId, setRecordingId } = useApp();
  const [schema, setSchema] = useState<number | null>(null);
  const [n, setN] = useState(20);
  const [busy, setBusy] = useState(false);
  const run = async (fn: () => Promise<unknown>, msg: string) => {
    setBusy(true);
    const r = await act(fn);
    setBusy(false);
    if (r !== undefined) notify("info", msg);
    refresh();
    onChange();
  };
  return (
    <Card eyebrow="Demo controls" title="Stage the story" bodyClass="card-b grid gap-3">
      <div className="flex flex-wrap gap-2 items-center">
        <span className="label m-0 w-full">Stockroom contract</span>
        <button className="btn" disabled={busy} aria-pressed={schema === 1} onClick={() => run(async () => { await api.stockroomSchema(1); setSchema(1); return true; }, "Stockroom is on contract v1")}>
          v1
        </button>
        <button
          className="btn"
          style={schema === 2 ? { borderColor: "var(--bad)", color: "var(--bad)" } : undefined}
          disabled={busy}
          onClick={() => run(async () => { await api.stockroomSchema(2); setSchema(2); return true; }, "Break 1: Stockroom switched to contract v2")}
        >
          Switch to v2 (Break 1)
        </button>
      </div>
      <div className="flex flex-wrap gap-2 items-center">
        <span className="label m-0 w-full">Inbox</span>
        <input type="number" min={1} max={500} className="input w-[80px] num" value={n} onChange={(e) => setN(Number(e.target.value) || 1)} aria-label="Messages to deliver" />
        <button className="btn" disabled={busy} onClick={() => run(() => api.deliver(n), `Delivered ${n} new messages`)}>
          Deliver new messages
        </button>
        <button className="btn" disabled={busy} title="Stretch: a 4th supplier in a new format" onClick={() => run(() => api.newSupplier(5), "Break 2: 5 messages from a new supplier")}>
          New supplier (Break 2)
        </button>
      </div>
      <div className="flex flex-wrap gap-2 items-center">
        <span className="label m-0 w-full">Everything</span>
        <button
          className="btn btn-danger"
          disabled={busy}
          onClick={() => {
            if (!window.confirm("Reset the Inbox, Stockroom and all integrations, runs and incidents?")) return;
            void run(async () => {
              await api.demoReset();
              setSchema(1);
              setRunId(null);
              setThreadId(null);
              setRecordingId(null);
              return true;
            }, "Demo reset");
          }}
        >
          Reset demo
        </button>
      </div>
    </Card>
  );
}

export default function RunsTab() {
  const { settings, integrationName: name, integrations, incidents, runId, setRunId, act, refresh, go } = useApp();
  const integ = integrations.find((i) => i.name === name);
  const [dry, setDry] = useState(false);
  const [limit, setLimit] = useState<string>("100");
  const [bump, setBump] = useState(0);
  const runsQ = usePoll(() => api.runs({ integration: name, limit: 50 }), 2000, [name]);
  const runs = runsQ.data ?? [];
  const live = useRunLive(runId, () => {
    setBump((b) => b + 1);
    refresh();
    void runsQ.reload();
  });
  const ai = !!settings?.ai_enabled;
  const c = splitCounts(live.counts);
  const total = live.progress?.total;
  const processed = live.running ? live.progress?.processed ?? 0 : live.detail?.records.length ?? 0;
  const llmTotal = runs.reduce((s, r) => s + (r.llm_calls ?? 0), 0);
  const thisRunLlm = live.detail?.run.llm_calls ?? 0;

  const start = async () => {
    const r = await act(() => api.startRun({ integration: name, dry_run: dry, limit: limit ? Number(limit) : undefined }));
    if (r) {
      setRunId(r.run_id);
      void runsQ.reload();
    }
  };
  const replay = async () => {
    const r = await act(() => api.replay(name));
    if (r) setRunId(r.run_id);
  };

  // a blocked run's incident, or the newest open one
  const incidentId = incidents[0]?.id ?? null;
  const reasons = Object.entries(c.reasons).sort(([a], [b]) => REASON_ORDER.indexOf(a) - REASON_ORDER.indexOf(b));

  return (
    <>
      <PageHead
        eyebrow="Step 5 · run it, no model in the loop"
        title="Runs"
        right={
          <div className="flex items-center gap-4 flex-wrap">
            <AiToggle big />
            <div
              className="rounded-md border-2 px-5 py-2 grid text-center"
              style={{ borderColor: "var(--ok)", background: "var(--ok-soft)" }}
              title="Runs execute generated code in a subprocess whose environment has no model keys"
            >
              <span className="font-mono text-[12px] font-semibold uppercase tracking-wider" style={{ color: "var(--ok)" }}>LLM calls</span>
              <b className="display text-[48px] font-extrabold leading-none num" style={{ color: "var(--ok)" }} data-testid="llm-calls">
                {thisRunLlm}
              </b>
              <span className="font-mono text-[11px] text-muted">{runs.length ? `this run · ${llmTotal} across ${runs.length} runs` : "per run, always"}</span>
            </div>
          </div>
        }
      >
        {ai
          ? "AI is on, but runs never use it: the batch runs the generated program in a sandboxed subprocess with no model keys."
          : "AI is off. Runs work exactly the same: only building and repairing need the agents."}
      </PageHead>

      {incidentId != null && <IncidentBanner key={incidentId} id={incidentId} />}

      <div className="grid gap-6 lg:grid-cols-[minmax(0,380px)_minmax(0,1fr)]">
        <div className="grid gap-6 content-start">
          <Card eyebrow="Run batch" title={integ ? `${name}` : "No integration yet"} right={integ?.active_version != null && <Pill k="success">active v{integ.active_version}</Pill>} bodyClass="card-b grid gap-3">
            {!integ ? (
              <Empty action={<button className="btn" onClick={() => go("record")}>Go to Record</button>}>Build and approve a version first.</Empty>
            ) : (
              <>
                <label className="flex items-center gap-2.5 cursor-pointer">
                  <input type="checkbox" className="w-5 h-5" checked={dry} onChange={(e) => setDry(e.target.checked)} />
                  <span>Dry run <span className="text-muted text-sm">(staging Stockroom, nothing written live)</span></span>
                </label>
                <div className="flex items-center gap-2">
                  <label className="label m-0" htmlFor="limit">Limit</label>
                  <input id="limit" type="number" min={1} className="input w-[110px] num" placeholder="all" value={limit} onChange={(e) => setLimit(e.target.value)} />
                  <span className="text-sm text-muted">blank = every new record</span>
                </div>
                <div className="flex gap-2 flex-wrap">
                  <button className="btn btn-primary btn-lg" onClick={start} disabled={integ.active_version == null || live.running}>
                    ▶ Run batch{limit ? ` (${limit})` : ""}
                  </button>
                  <button className="btn" onClick={replay} disabled={integ.active_version == null || live.running} title="Re-run quarantined records on the active version">
                    Replay quarantined
                  </button>
                </div>
              </>
            )}
          </Card>
          <DemoControls onChange={() => { setBump((b) => b + 1); void runsQ.reload(); }} />
        </div>

        <Card
          eyebrow={live.detail ? `Run #${live.detail.run.id} · v${live.detail.run.version} · ${live.detail.run.dry_run ? "dry run" : "live"}` : "Live counters"}
          title={
            live.status === "blocked" ? "Blocked by preflight: 0 records written" :
            live.running ? "Running…" : live.status ? `Run ${live.status}` : "No run selected"
          }
          right={
            live.status && (
              <span className="font-mono text-[15px] num">
                ⏱ {fmtDuration(live.duration)}
              </span>
            )
          }
          bodyClass="card-b grid gap-4"
        >
          {!live.detail ? (
            <p className="m-0 text-muted">Start a batch, or pick a run below.</p>
          ) : (
            <>
              <div className="flex flex-wrap gap-3">
                <Counter label="success" value={c.success} k="success" big sub="written to Stockroom" />
                <Counter label="duplicates skipped" value={c.duplicate} k="duplicate" big sub="resends, deduped on PO" />
                <Counter label="quarantined" value={c.quarantined} k="quarantined" big sub="held for review" />
              </div>
              {reasons.length > 0 && (
                <div className="flex flex-wrap gap-2">
                  {reasons.map(([r, n]) => (
                    <Pill key={r} k={r}>{r} <span className="num">{n}</span></Pill>
                  ))}
                </div>
              )}
              <div className="grid gap-1">
                <div className="flex justify-between font-mono text-xs text-muted">
                  <span>{processed}{total != null ? ` / ${total}` : ""} records processed</span>
                  <span>{live.detail.run.started_at && `started ${fmtTime(live.detail.run.started_at)}`}</span>
                </div>
                <div className="h-2.5 rounded bg-sunken overflow-hidden">
                  <div
                    className="h-full transition-all"
                    style={{
                      width: `${total ? (processed / total) * 100 : live.running ? 5 : 100}%`,
                      background: live.status === "blocked" ? "var(--bad)" : "var(--accent)",
                    }}
                  />
                </div>
              </div>
              {live.status === "blocked" && live.detail.run.message && (
                <p className="m-0 font-mono text-sm" style={{ color: "var(--bad)" }}>{live.detail.run.message}</p>
              )}
              {!live.running && live.detail.records.length > 0 && <RecordGrid records={live.detail.records} />}
            </>
          )}
        </Card>
      </div>

      {live.detail && !live.running && live.detail.records.length > 0 && (
        <Card eyebrow={`Run #${live.detail.run.id}`} title="Records and reason codes">
          <RecordsTable records={live.detail.records} />
        </Card>
      )}

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
        <Card eyebrow="Benchmark" title="Accuracy vs ground truth">
          <AccuracyFigure name={name} bump={bump} />
        </Card>
        <Card eyebrow="History" title="Runs" bodyClass="overflow-x-auto max-h-[360px] overflow-y-auto">
          {runs.length === 0 ? (
            <div className="p-4 text-muted">No runs yet.</div>
          ) : (
            <table className="tbl">
              <thead>
                <tr><th>Run</th><th>Ver</th><th>Mode</th><th>Status</th><th>✓ / dup / quar</th><th>Time</th><th>LLM</th></tr>
              </thead>
              <tbody>
                {runs.map((r) => {
                  const k = splitCounts(r.counts);
                  return (
                    <tr key={r.id} onClick={() => setRunId(r.id)} className="cursor-pointer" style={{ background: r.id === runId ? "var(--accent-soft)" : undefined }}>
                      <td className="font-mono">#{r.id}</td>
                      <td className="font-mono">v{r.version}</td>
                      <td>{r.dry_run ? "dry" : "live"}</td>
                      <td><Pill k={r.status === "completed" ? "success" : r.status === "running" ? "duplicate" : "quarantined"}>{r.status}</Pill></td>
                      <td className="font-mono num">{k.success} / {k.duplicate} / {k.quarantined}</td>
                      <td className="font-mono num">{fmtDuration(r.duration_s)}</td>
                      <td className="font-mono font-semibold" style={{ color: "var(--ok)" }}>{r.llm_calls}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </Card>
      </div>
    </>
  );
}
