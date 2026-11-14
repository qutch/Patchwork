import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent, type ReactNode } from "react";
import { api, ApiError, type BenchKind, type BenchResults, type BreakResult, type Metric } from "../api";
import {
  breakEven, breakRows, caughtTally, costAt, costModels, fmtMetric, fmtMinutes, kindLabel, niceTicks, readout,
  repairRows, segments, tableRows, val, type CostModel, type LineStyle, type SeriesId,
} from "../lib/bench";
import { Card, Empty, PageHead } from "../components/ui";

const SERIES_COLOR: Record<SeriesId, string> = { patchwork: "var(--s-pw)", manual: "var(--s-manual)", computer_use: "var(--s-cu)" };
const DASH: Record<LineStyle, string | undefined> = { measured: undefined, extrapolated: "8 5", estimate: "2 5" };
const STYLE_WORD: Record<LineStyle, string> = { measured: "measured", extrapolated: "extrapolated", estimate: "estimate" };

// ------------------------------------------------------------------------------- small pieces
export function KindBadge({ kind, title }: { kind: BenchKind | string; title?: string }) {
  const soft = kind !== "measured" && kind !== "measured (earlier run)";
  const prefix = kind === "estimate" ? "≈ " : kind === "extrapolated" ? "↗ " : kind === "not measured" ? "∅ " : "";
  return (
    <span
      className="pill"
      title={title}
      style={soft
        ? { color: "var(--muted)", background: "transparent", border: `1px ${kind === "not measured" ? "dotted" : "dashed"} var(--muted)`, padding: "3px 6px" }
        : { color: "var(--ink)", background: "var(--sunken)", border: "1px solid transparent", padding: "3px 6px" }}
    >
      {prefix}{kindLabel(kind)}
    </span>
  );
}

function Value({ m, big = false }: { m: Metric | null | undefined; big?: boolean }) {
  if (!m) return <span className="text-muted">—</span>;
  const soft = m.kind !== "measured" && m.kind !== "measured (earlier run)";
  return (
    <span className={big ? "text-[22px] font-semibold leading-none" : "font-semibold"} style={{ color: soft ? "var(--muted)" : "var(--ink)" }} title={`${m.kind}: ${m.source}${m.note ? ` (${m.note})` : ""}`}>
      {soft && val(m) != null && m.kind !== "not measured" ? "≈ " : ""}{fmtMetric(m)}
    </span>
  );
}

function LineKey({ color, style, width = 26 }: { color: string; style: LineStyle; width?: number }) {
  return (
    <svg width={width} height="10" aria-hidden className="inline-block align-middle shrink-0">
      <line x1="1" y1="5" x2={width - 1} y2="5" stroke={color} strokeWidth="2" strokeDasharray={DASH[style]} strokeLinecap="round" />
    </svg>
  );
}

function useWidth<T extends HTMLElement>(fallback: number): [React.RefObject<T>, number] {
  const ref = useRef<T>(null);
  const [w, setW] = useState(fallback);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    setW(el.clientWidth || fallback);
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => setW(el.clientWidth || fallback));
    ro.observe(el);
    return () => ro.disconnect();
  }, [fallback]);
  return [ref, w];
}

// ------------------------------------------------------------------------------- break-even chart
function BreakEvenChart({ models, xMax, label, showBreakEven }: { models: CostModel[]; xMax: number; label: string; showBreakEven: boolean }) {
  const [wrap, width] = useWidth<HTMLDivElement>(560);
  const narrow = width < 460;
  const H = 260;
  const M = { l: 46, r: narrow ? 14 : 156, t: 14, b: 36 };
  const iw = Math.max(80, width - M.l - M.r);
  const ih = H - M.t - M.b;

  const yTop = Math.max(...models.map((m) => costAt(m, xMax)), 1);
  const yTicks = niceTicks(yTop, 4);
  const yMax = yTicks[yTicks.length - 1];
  const xTicks = niceTicks(xMax, narrow ? 4 : 5).filter((t) => t <= xMax);
  const sx = (x: number) => M.l + (x / xMax) * iw;
  const sy = (y: number) => M.t + ih - (y / yMax) * ih;

  const manual = models.find((m) => m.id === "manual");
  const pw = models.find((m) => m.id === "patchwork");
  const be = showBreakEven ? breakEven(pw, manual) : null;

  // End labels on the right, nudged apart and tied back with leader lines.
  const ends = useMemo(() => {
    const items = models.map((m) => {
      const segs = segments(m, xMax);
      return { m, y: sy(costAt(m, xMax)), style: segs[segs.length - 1]?.style ?? "measured" };
    }).sort((a, b) => a.y - b.y);
    let prev = -Infinity;
    return items.map((it) => {
      const ly = Math.max(it.y, prev + 30);
      prev = ly;
      return { ...it, ly };
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [models, xMax, width]);

  const [hx, setHx] = useState<number | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const onMove = (e: PointerEvent<SVGRectElement>) => {
    const box = svgRef.current?.getBoundingClientRect();
    if (!box) return;
    const x = Math.round(((e.clientX - box.left - M.l) / iw) * xMax);
    setHx(Math.min(xMax, Math.max(0, x)));
  };
  const onKey = (e: KeyboardEvent<SVGSVGElement>) => {
    const step = e.shiftKey ? Math.max(1, Math.round(xMax / 10)) : 1;
    if (e.key === "ArrowRight") setHx((x) => Math.min(xMax, (x ?? 0) + step));
    else if (e.key === "ArrowLeft") setHx((x) => Math.max(0, (x ?? 0) - step));
    else if (e.key === "Escape") setHx(null);
    else return;
    e.preventDefault();
  };
  const read = hx == null ? [] : readout(models, hx).sort((a, b) => b.minutes - a.minutes);

  return (
    <figure className="m-0 grid gap-2 min-w-0">
      <figcaption className="text-sm font-semibold">{label}</figcaption>
      <div ref={wrap} className="relative min-w-0">
        <svg
          ref={svgRef}
          width={width}
          height={H}
          role="img"
          aria-label={`${label}: cumulative minutes by records processed. ${models.map((m) => `${m.label} ${fmtMinutes(costAt(m, xMax))} at ${xMax}`).join("; ")}${be ? `; break-even at ${be} records` : ""}`}
          tabIndex={0}
          onKeyDown={onKey}
          onBlur={() => setHx(null)}
          style={{ display: "block", overflow: "visible", font: "11px var(--mono)" }}
        >
          {/* grid + axes: solid hairlines */}
          {yTicks.map((t) => (
            <g key={t}>
              <line x1={M.l} x2={M.l + iw} y1={sy(t)} y2={sy(t)} stroke={t === 0 ? "var(--line)" : "var(--grid)"} strokeWidth="1" />
              <text x={M.l - 6} y={sy(t)} dy="0.32em" textAnchor="end" fill="var(--muted)" className="num">{t.toLocaleString("en-US")}</text>
            </g>
          ))}
          {xTicks.map((t) => (
            <text key={t} x={sx(t)} y={M.t + ih + 16} textAnchor="middle" fill="var(--muted)" className="num">{t}</text>
          ))}
          <text x={M.l + iw / 2} y={H - 2} textAnchor="middle" fill="var(--muted)">records processed</text>
          <text x={12} y={M.t + ih / 2} textAnchor="middle" fill="var(--muted)" transform={`rotate(-90 12 ${M.t + ih / 2})`}>minutes</text>

          {/* series */}
          {models.map((m) => (
            <g key={m.id}>
              {segments(m, xMax).map((s, i) => (
                <line
                  key={i}
                  x1={sx(s.x0)} y1={sy(s.y0)} x2={sx(s.x1)} y2={sy(s.y1)}
                  stroke={SERIES_COLOR[m.id]} strokeWidth="2" strokeLinecap="round" strokeDasharray={DASH[s.style]}
                />
              ))}
            </g>
          ))}

          {/* Patchwork setup: the cost paid before record 1 */}
          {pw && showBreakEven && (
            <g>
              <circle cx={sx(0)} cy={sy(pw.setupMin)} r="4" fill={SERIES_COLOR.patchwork} stroke="var(--surface)" strokeWidth="2" />
              <text x={sx(0) + 8} y={sy(pw.setupMin) - 8} fill="var(--muted)">setup {fmtMinutes(pw.setupMin)}</text>
            </g>
          )}
          {pw && pw.steps.filter((s) => s.x < xMax).slice(0, 1).map((s) => (
            <text key={s.label} x={sx(s.x) - 6} y={sy(costAt(pw, s.x + 1)) - 10} textAnchor="end" fill="var(--muted)">repairs (Break 1, 2)</text>
          ))}

          {/* break-even */}
          {be != null && be <= xMax && manual && (
            <g>
              <line x1={sx(be)} x2={sx(be)} y1={sy(costAt(manual, be))} y2={M.t + ih} stroke="var(--muted)" strokeWidth="1" />
              <circle cx={sx(be)} cy={sy(costAt(manual, be))} r="5" fill="var(--ink)" stroke="var(--surface)" strokeWidth="2" />
              <text x={sx(be) + 8} y={sy(costAt(manual, be)) + 16} fill="var(--ink)" fontWeight="600">break-even: {be} records</text>
            </g>
          )}

          {/* direct end labels (legend below carries identity too) */}
          {!narrow && ends.map((e) => (
            <g key={e.m.id}>
              {Math.abs(e.ly - e.y) > 2 && <line x1={M.l + iw + 2} y1={e.y} x2={M.l + iw + 8} y2={e.ly} stroke="var(--muted)" strokeWidth="1" />}
              <line x1={M.l + iw + 10} x2={M.l + iw + 22} y1={e.ly} y2={e.ly} stroke={SERIES_COLOR[e.m.id]} strokeWidth="2" strokeDasharray={DASH[e.style]} strokeLinecap="round" />
              <text x={M.l + iw + 26} y={e.ly} dy="0.32em" fill="var(--ink)">{e.m.label === "Computer-use agent" ? "Computer-use" : e.m.label}</text>
              <text x={M.l + iw + 26} y={e.ly + 13} dy="0.32em" fill="var(--muted)">{fmtMinutes(costAt(e.m, xMax))}{e.style !== "measured" ? ` · ${STYLE_WORD[e.style]}` : ""}</text>
            </g>
          ))}

          {/* hover layer */}
          {hx != null && <line x1={sx(hx)} x2={sx(hx)} y1={M.t} y2={M.t + ih} stroke="var(--muted)" strokeWidth="1" />}
          {hx != null && models.map((m) => (
            <circle key={m.id} cx={sx(hx)} cy={sy(costAt(m, hx))} r="4" fill={SERIES_COLOR[m.id]} stroke="var(--surface)" strokeWidth="2" />
          ))}
          <rect x={M.l} y={M.t} width={iw} height={ih} fill="transparent" onPointerMove={onMove} onPointerLeave={() => setHx(null)} />
        </svg>
        {hx != null && (
          <div
            className="absolute pointer-events-none card shadow-md px-3 py-2 text-[12.5px] z-10"
            style={{ top: 8, left: sx(hx) > width / 2 ? Math.max(0, sx(hx) - 262) : Math.min(sx(hx) + 12, Math.max(0, width - 250)), width: 250 }}
          >
            <div className="font-mono text-xs text-muted mb-1">{hx} records</div>
            {read.map((r) => (
              <div key={r.id} className="flex items-center gap-2">
                <LineKey color={SERIES_COLOR[r.id]} style={r.style} width={16} />
                <b className="num whitespace-nowrap">{fmtMinutes(r.minutes)}</b>
                <span className="text-muted truncate">{r.id === "computer_use" ? "Computer-use" : r.label}{r.style !== "measured" ? ` (${r.style})` : ""}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </figure>
  );
}

function ChartLegend({ models }: { models: CostModel[] }) {
  return (
    <div className="flex flex-wrap gap-x-5 gap-y-2 text-[13px]">
      {models.map((m) => {
        const style: LineStyle = m.kind === "estimate" || m.kind === "not measured" ? "estimate" : "measured";
        return (
          <span key={m.id} className="inline-flex items-center gap-2">
            <LineKey color={SERIES_COLOR[m.id]} style={style} />
            <span><b>{m.label}</b> <span className="text-muted">· {m.whose}</span></span>
          </span>
        );
      })}
      <span className="inline-flex items-center gap-3 text-muted font-mono text-xs ml-auto">
        <span className="inline-flex items-center gap-1"><LineKey color="var(--muted)" style="measured" width={18} /> measured</span>
        <span className="inline-flex items-center gap-1"><LineKey color="var(--muted)" style="extrapolated" width={18} /> extrapolated</span>
        <span className="inline-flex items-center gap-1"><LineKey color="var(--muted)" style="estimate" width={18} /> estimate</span>
      </span>
    </div>
  );
}

function ChartTable({ models }: { models: CostModel[] }) {
  const xs = [0, 1, 5, 10, 20, 50, 100, 150, 200];
  return (
    <details className="text-sm">
      <summary className="cursor-pointer text-muted font-mono text-xs">Chart data as a table</summary>
      <div className="overflow-x-auto mt-2">
        <table className="tbl num">
          <thead>
            <tr><th>Records</th>{models.map((m) => <th key={m.id}>{m.label}</th>)}</tr>
          </thead>
          <tbody>
            {xs.map((x) => (
              <tr key={x}>
                <td>{x}</td>
                {readout(models, x).map((r) => (
                  <td key={r.id}>{fmtMinutes(r.minutes)}{r.style !== "measured" && <span className="text-muted"> · {r.style}</span>}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

// ------------------------------------------------------------------------------- breaks
function Verdict({ b }: { b: BreakResult }) {
  const caught = b.caught_before_bad_write && b.detected;
  const silent = !b.detected && (val(b.silently_dropped) ?? 0) > 0;
  const [fg, bg, text] = caught
    ? ["var(--ok)", "var(--ok-soft)", "✓ caught before any bad write"]
    : silent
      ? ["var(--bad)", "var(--bad-soft)", "✗ not caught: records silently dropped"]
      : (val(b.bad_writes) ?? 0) > 0
        ? ["var(--bad)", "var(--bad-soft)", "✗ bad writes before anyone noticed"]
        : ["var(--val)", "var(--val-soft)", "◐ no bad write, but not flagged as this break"];
  return <span className="pill" style={{ color: fg, background: bg }}>{text}</span>;
}

function BreakSide({ who, b }: { who: string; b: BreakResult }) {
  const cell = (label: string, m: Metric) => (
    <div className="grid gap-0.5">
      <span className="eyebrow">{label}</span>
      <Value m={m} />
    </div>
  );
  return (
    <div className="grid gap-2.5 content-start border border-line rounded-md p-3 min-w-0">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <b>{who}</b>
        <Verdict b={b} />
      </div>
      <div className="grid grid-cols-3 gap-2 num">
        {cell("bad writes", b.bad_writes)}
        {cell("silently dropped", b.silently_dropped)}
        {cell("held + flagged", b.held)}
      </div>
      <div className="grid grid-cols-2 gap-2">
        {cell("time to detect", b.time_to_detect_s)}
        <div className="grid gap-0.5">
          <span className="eyebrow">incident</span>
          <span className="font-mono text-[13px]">{b.incident_type ?? "none"}</span>
        </div>
      </div>
      <p className="m-0 text-[13.5px]">{b.outcome}</p>
      <p className="m-0 text-[12.5px] text-muted"><span className="font-semibold">What a human sees:</span> {b.alert}</p>
    </div>
  );
}

// ------------------------------------------------------------------------------- page
function Stat({ label, children, sub }: { label: string; children: ReactNode; sub?: ReactNode }) {
  return (
    <div className="card card-b grid gap-1.5 content-start">
      <span className="eyebrow">{label}</span>
      <div>{children}</div>
      {sub && <div className="text-[12.5px] text-muted">{sub}</div>}
    </div>
  );
}

function useBench() {
  const [data, setData] = useState<BenchResults | null>(null);
  const [error, setError] = useState<{ status: number; detail: string } | null>(null);
  const [loading, setLoading] = useState(true);
  const load = () => {
    setLoading(true);
    api.benchResults().then(
      (d) => { setData(d); setError(null); setLoading(false); },
      (e) => { setError({ status: e instanceof ApiError ? e.status : 0, detail: e instanceof Error ? e.message : String(e) }); setLoading(false); },
    );
  };
  useEffect(load, []);
  return { data, error, loading, reload: load };
}

export default function BenchmarkTab() {
  const { data, error, loading, reload } = useBench();
  const head = (
    <PageHead
      eyebrow="Benchmark"
      title="Why not a one-shot script?"
      right={<button className="btn" onClick={reload} disabled={loading}>Reload</button>}
    >
      Setup cost, run cost and what happens when things break: Patchwork against doing it by hand, a computer-use agent,
      and a script written by asking Claude once. Every number says whether it was measured, extrapolated or estimated.
    </PageHead>
  );
  if (!data) {
    return (
      <>
        {head}
        <Empty action={error && error.status !== 404 ? <button className="btn" onClick={reload}>Try again</button> : undefined}>
          {loading ? "Loading benchmark results…"
            : error?.status === 404 ? <>No benchmark results yet. Run <code className="font-mono">make bench</code> (or <code className="font-mono">python -m bench.run</code>) to write <code className="font-mono">bench/results.json</code>, then reload.</>
              : `Couldn't load benchmark results: ${error?.detail ?? "unknown error"}`}
        </Empty>
      </>
    );
  }
  return <BenchBody r={data} head={head} />;
}

export function BenchBody({ r, head }: { r: BenchResults; head?: ReactNode }) {
  const models = useMemo(() => costModels(r), [r]);
  const pw = models.find((m) => m.id === "patchwork");
  const manual = models.find((m) => m.id === "manual");
  const be = breakEven(pw, manual);
  const tally = caughtTally(r);
  const rows = breakRows(r);
  const repairs = repairRows(r);
  const table = tableRows(r);
  const groups = [...new Set(table.map((t) => t.group))];
  const d = r.patchwork.deep_agent;

  return (
    <>
      {head}

      <div className="grid gap-3 grid-cols-1 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Break-even vs manual" sub={manual ? `manual rate is ${manual.kind === "measured" ? "measured" : "an " + manual.kind}` : "manual rate not available"}>
          <span className="text-[22px] font-semibold leading-none">{be != null ? `${be} records` : "—"}</span>
        </Stat>
        <Stat label="Breaks caught before a bad write" sub={`one-shot script: ${tally.baseline_script} of ${tally.total}`}>
          <span className="text-[22px] font-semibold leading-none">{tally.patchwork} of {tally.total}</span>
        </Stat>
        <Stat label={`Run of ${val(r.patchwork.run.records) ?? "?"} records`} sub={<>{fmtMetric(r.patchwork.run.per_record_ms)} per record · accuracy {fmtMetric(r.patchwork.run.accuracy)}</>}>
          <Value m={r.patchwork.run.wall_s} big />
        </Stat>
        <Stat label="LLM calls while running" sub="model calls happen only in BUILD and REPAIR">
          <Value m={r.patchwork.run.llm_calls} big />
        </Stat>
      </div>

      <Card
        eyebrow="Break-even"
        title="Minutes until N records are in the Stockroom"
        right={<span className="font-mono text-xs text-muted">generated {new Date(r.generated_at).toLocaleString()}</span>}
      >
        <div className="grid gap-4">
          <ChartLegend models={models} />
          <div className="grid gap-6 lg:grid-cols-2">
            <BreakEvenChart models={models} xMax={20} label="First 20 records: where the lines cross" showBreakEven />
            <BreakEvenChart models={models} xMax={200} label="First 200 records, including both breaks" showBreakEven={false} />
          </div>
          <div className="text-[13px] text-muted grid gap-1 max-w-[110ch]">
            <p className="m-0">
              Manual minutes are human minutes. Computer-use minutes are agent wall-clock minutes (someone pays for and usually
              watches them). Patchwork pays a one-off setup ({pw ? pw.setupParts.map((p) => `${p.label} ${fmtMinutes(p.minutes)}, ${p.kind}`).join(" + ") : "n/a"}),
              then runs unattended: its per-record minutes are machine time with zero LLM calls. Setup uses the deep agent's BUILD
              time from an earlier real-model run rather than the scripted agent's, and each break adds the deep agent's repair time.
            </p>
            <p className="m-0">
              Solid = measured. Dashed = extrapolated from a measured rate past the records actually measured. Dotted = an estimate
              (see the assumption in the table below). Hover or focus a chart and use ← → to read every line at one record count.
            </p>
          </div>
          <ChartTable models={models} />
        </div>
      </Card>

      <Card
        eyebrow="Maintenance"
        title="Breaks caught before any bad write"
        right={
          <span className="font-mono text-[13px]">
            Patchwork <b>{tally.patchwork}/{tally.total}</b> · one-shot script <b>{tally.baseline_script}/{tally.total}</b>
          </span>
        }
      >
        <div className="grid gap-5">
          {rows.map((row) => (
            <section key={row.key} className="grid gap-2">
              <h3 className="text-[15px] font-bold m-0">
                {row.name} <span className="font-mono text-xs text-muted font-normal">· {row.records} records affected</span>
              </h3>
              <div className="grid gap-3 md:grid-cols-2">
                <BreakSide who="Patchwork" b={row.patchwork} />
                <BreakSide who="One-shot script from Claude" b={row.baseline} />
              </div>
            </section>
          ))}
          <p className="m-0 text-[12.5px] text-muted">
            The script is <code className="font-mono">{r.baseline_script.script}</code>, written from <code className="font-mono">{r.baseline_script.prompt}</code>. {r.baseline_script.how_written}
          </p>
        </div>
      </Card>

      <Card eyebrow="Repair time" title="From a break to an approved, tested version">
        <div className="overflow-x-auto">
          <table className="tbl">
            <thead>
              <tr>
                <th>Step</th>
                <th>Scripted agent · this run</th>
                <th>Deep agent · {d.date}</th>
                <th>Model calls</th>
              </tr>
            </thead>
            <tbody>
              {repairs.map((row) => (
                <tr key={row.label}>
                  <td>{row.label}</td>
                  <td className="num"><Value m={row.scripted} /> {row.scripted && <KindBadge kind={row.scripted.kind} title={row.scripted.source} />}</td>
                  <td className="num"><Value m={row.deep} /> {row.deep && <KindBadge kind={row.deep.kind} title={row.deep.source} />}</td>
                  <td className="num"><Value m={row.deepCalls} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="m-0 mt-3 text-[12.5px] text-muted">
          Scripted agent: no model, the harness answers its questions and approves instantly, so this is the machine cost of
          compile + generated tests + deploy. Deep agent: {d.model}, a real model run, questions answered by a script; source {d.source}.
        </p>
      </Card>

      <Card eyebrow="All numbers" title="Headline numbers and where they come from" bodyClass="">
        <div className="overflow-x-auto">
          <table className="tbl">
            <thead>
              <tr><th>Metric</th><th>Value</th><th>Kind</th><th className="hidden md:table-cell">Source</th></tr>
            </thead>
            {groups.map((g) => (
              <tbody key={g}>
                <tr><td colSpan={4} className="font-semibold" style={{ background: "var(--sunken)" }}>{g}</td></tr>
                {table.filter((t) => t.group === g).map((t) => (
                  <tr key={g + t.label}>
                    <td>{t.label}</td>
                    <td className="num whitespace-nowrap"><Value m={t.metric} /></td>
                    <td><KindBadge kind={t.metric.kind} /></td>
                    <td className="hidden md:table-cell text-muted text-[12.5px] max-w-[52ch]">{t.metric.source}{t.metric.note ? ` (${t.metric.note})` : ""}</td>
                  </tr>
                ))}
              </tbody>
            ))}
          </table>
        </div>
      </Card>

      {r.scenario.notes.length > 0 && (
        <Card eyebrow="Scenario" title="What was run">
          <ul className="m-0 pl-5 grid gap-1 text-[13.5px]">
            <li>{r.scenario.seed_records} seed messages; Patchwork's measured run is the first {r.scenario.run_records}.</li>
            <li>{r.scenario.break1}</li>
            <li>{r.scenario.break2}</li>
            {r.scenario.notes.map((n) => <li key={n}>{n}</li>)}
          </ul>
          <p className="m-0 mt-2 font-mono text-xs text-muted">{r.command}</p>
        </Card>
      )}
    </>
  );
}
