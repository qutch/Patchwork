// Data shaping for the Benchmark tab (bench/results.json). Pure functions, unit-tested in bench.test.ts.
//
// Every number in results.json carries a `kind`. Anything that is not a measurement must look different:
// lines are "measured" (solid), "extrapolated" (measured rate carried past what was measured) or
// "estimate" (a stated assumption); both of the latter are drawn dashed and labelled.

import type { BenchKind, BenchResults, BenchV2Results, BreakResult, Metric } from "../api";

export type LineStyle = "measured" | "extrapolated" | "estimate";
export type SeriesId = "manual" | "computer_use" | "patchwork";

/** The numeric value of a metric, or null when it is missing or "not measured". */
export function val(m: Metric | null | undefined): number | null {
  return m && typeof m.value === "number" && Number.isFinite(m.value) ? m.value : null;
}

/** True for kinds that must not be presented as a measurement. */
export function isSoftKind(kind: BenchKind | string | undefined): boolean {
  return kind !== "measured" && kind !== "measured (earlier run)";
}

/** Line style for a value of this kind (a measured rate used past its sample is "extrapolated"). */
export function styleForKind(kind: BenchKind | string | undefined): LineStyle {
  if (kind === "measured" || kind === "measured (earlier run)") return "measured";
  if (kind === "extrapolated") return "extrapolated";
  return "estimate";
}

// ------------------------------------------------------------------------------- cost models
export interface Step {
  /** applied after this many records (a break arrives before record x+1) */
  x: number;
  minutes: number;
  label: string;
  kind: BenchKind;
}
export interface SetupPart {
  label: string;
  minutes: number;
  kind: BenchKind;
}
export interface CostModel {
  id: SeriesId;
  label: string;
  /** who spends the minutes; goes in legends and tooltips */
  whose: string;
  setupMin: number;
  setupParts: SetupPart[];
  perRecordMin: number;
  /** kind of the per-record rate */
  kind: BenchKind;
  /** records actually measured at this rate; beyond it the line is extrapolated. null = nothing measured */
  measuredUpTo: number | null;
  steps: Step[];
}

/** Cumulative minutes after processing `x` records. */
export function costAt(m: CostModel, x: number): number {
  let y = m.setupMin + m.perRecordMin * x;
  for (const s of m.steps) if (x > s.x) y += s.minutes;
  return y;
}

/** The models behind the break-even chart. A system whose per-record rate is null is left out. */
export function costModels(r: BenchResults): CostModel[] {
  const out: CostModel[] = [];

  const manualRate = val(r.manual?.per_record_min);
  if (manualRate != null) {
    const kind = r.manual.per_record_min.kind;
    out.push({
      id: "manual", label: "Manual", whose: "human minutes",
      setupMin: 0, setupParts: [], perRecordMin: manualRate, kind,
      measuredUpTo: kind === "measured" ? r.manual.timings_s?.length || null : null,
      steps: [],
    });
  }

  const cuRate = val(r.computer_use?.per_record_min);
  if (cuRate != null) {
    const kind = r.computer_use.per_record_min.kind;
    const setup = val(r.computer_use.setup_min) ?? 0;
    out.push({
      id: "computer_use", label: "Computer-use agent", whose: "agent wall-clock minutes",
      setupMin: setup,
      setupParts: setup ? [{ label: "setup", minutes: setup, kind: r.computer_use.setup_min.kind }] : [],
      perRecordMin: cuRate, kind,
      measuredUpTo: kind === "measured" ? r.computer_use.timings_s?.length || null : null,
      steps: [],
    });
  }

  const pw = r.patchwork;
  const perMs = val(pw?.run?.per_record_ms);
  if (pw && perMs != null) {
    const deepBuild = val(pw.deep_agent?.build_s);
    const buildS = deepBuild ?? val(pw.build?.wall_s) ?? 0;
    const buildKind = deepBuild != null ? pw.deep_agent.build_s.kind : pw.build.wall_s.kind;
    const human = val(pw.setup_human_min);
    const setupParts: SetupPart[] = [
      { label: deepBuild != null ? "BUILD (deep agent)" : `BUILD (${pw.agent} agent)`, minutes: buildS / 60, kind: buildKind },
    ];
    if (human != null) setupParts.push({ label: "human: record + answer + approve", minutes: human, kind: pw.setup_human_min.kind });

    const seed = r.scenario?.seed_records ?? 0;
    const steps: Step[] = [];
    const repair = (key: "break1" | "break2", deepS: Metric | undefined): Step | null => {
      const deep = val(deepS);
      const s = deep ?? val(pw.breaks?.[key]?.repair_s);
      if (s == null) return null;
      const kind = deep != null ? deepS!.kind : pw.breaks[key].repair_s.kind;
      return { x: 0, minutes: s / 60, label: `${key === "break1" ? "Break 1" : "Break 2"} repair`, kind };
    };
    const b1 = repair("break1", pw.deep_agent?.repair_break1_s);
    const b2 = repair("break2", pw.deep_agent?.repair_break2_s);
    if (b1) steps.push({ ...b1, x: seed });
    if (b2) steps.push({ ...b2, x: seed + (pw.breaks?.break1?.records_in_break ?? 0) });

    out.push({
      id: "patchwork", label: "Patchwork", whose: "setup, then unattended machine minutes",
      setupMin: setupParts.reduce((a, p) => a + p.minutes, 0), setupParts,
      perRecordMin: perMs / 60000, kind: pw.run.per_record_ms.kind,
      measuredUpTo: val(pw.run.records),
      steps,
    });
  }
  return out;
}

export interface Segment {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
  style: LineStyle;
  /** a vertical jump (setup or repair), not a per-record slope */
  jump?: string;
}

/** Piecewise segments for one model over [0, xMax], each tagged measured / extrapolated / estimate. */
export function segments(m: CostModel, xMax: number): Segment[] {
  const base = (x: number) => m.setupMin + m.perRecordMin * x;
  const stepsUpTo = (x: number, inclusive: boolean) =>
    m.steps.reduce((a, s) => a + (s.x < x || (inclusive && s.x === x) ? s.minutes : 0), 0);
  const restStyle: LineStyle = m.kind === "measured" || m.kind === "measured (earlier run)" ? "extrapolated" : styleForKind(m.kind);
  const cuts = new Set<number>([0, xMax]);
  if (m.measuredUpTo != null && m.measuredUpTo > 0 && m.measuredUpTo < xMax) cuts.add(m.measuredUpTo);
  for (const s of m.steps) if (s.x > 0 && s.x < xMax) cuts.add(s.x);
  const xs = [...cuts].sort((a, b) => a - b);

  const out: Segment[] = [];
  for (let i = 0; i < xs.length - 1; i++) {
    const a = xs[i], b = xs[i + 1];
    const mid = (a + b) / 2;
    const measured = m.measuredUpTo != null && mid <= m.measuredUpTo;
    out.push({
      x0: a, y0: base(a) + stepsUpTo(a, true), x1: b, y1: base(b) + stepsUpTo(b, false),
      style: measured ? styleForKind(m.kind) : restStyle,
    });
    const step = m.steps.find((s) => s.x === b && b < xMax);
    if (step) {
      const y = base(b) + stepsUpTo(b, false);
      out.push({ x0: b, y0: y, x1: b, y1: y + step.minutes, style: styleForKind(step.kind), jump: step.label });
    }
  }
  return out;
}

/** First record count N >= 1 at which `a` has cost no more than `b` (null if never within maxN). */
export function breakEven(a: CostModel | undefined, b: CostModel | undefined, maxN = 100_000): number | null {
  if (!a || !b) return null;
  for (let n = 1; n <= maxN; n++) if (costAt(a, n) <= costAt(b, n)) return n;
  return null;
}

/** Round up to a clean axis maximum and return ~`count` evenly spaced ticks from 0. */
export function niceTicks(max: number, count = 5): number[] {
  if (!(max > 0)) return [0, 1];
  const raw = max / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((f) => f * mag).find((s) => s >= raw) ?? 10 * mag;
  const ticks: number[] = [];
  for (let t = 0; t < max + step * 0.999; t += step) ticks.push(Math.round(t * 1e6) / 1e6);
  return ticks;
}

/** Values of every model at a given record count (tooltip and the chart's table view). */
export function readout(models: CostModel[], x: number): { id: SeriesId; label: string; minutes: number; style: LineStyle }[] {
  return models.map((m) => {
    const seg = segments(m, Math.max(x, 1)).filter((s) => !s.jump).find((s) => x >= s.x0 && x <= s.x1);
    return { id: m.id, label: m.label, minutes: costAt(m, x), style: seg?.style ?? styleForKind(m.kind) };
  });
}

// ------------------------------------------------------------------------------- breaks
export type BreakKey = "break1" | "break2";
export interface BreakRow {
  key: BreakKey;
  name: string;
  records: number;
  patchwork: BreakResult;
  baseline: BreakResult;
}

export function breakRows(r: BenchResults): BreakRow[] {
  return (["break1", "break2"] as const)
    .filter((k) => r.patchwork?.breaks?.[k] && r.baseline_script?.breaks?.[k])
    .map((k) => ({
      key: k,
      name: r.patchwork.breaks[k].name,
      records: r.patchwork.breaks[k].records_in_break,
      patchwork: r.patchwork.breaks[k],
      baseline: r.baseline_script.breaks[k],
    }));
}

/** "Breaks caught before any bad write": counted from the break results themselves. */
export function caughtTally(r: BenchResults): { total: number; patchwork: number; baseline_script: number } {
  const rows = breakRows(r);
  const caught = (b: BreakResult) => (b.caught_before_bad_write && b.detected ? 1 : 0);
  return {
    total: rows.length,
    patchwork: rows.reduce((a, row) => a + caught(row.patchwork), 0),
    baseline_script: rows.reduce((a, row) => a + caught(row.baseline), 0),
  };
}

// ------------------------------------------------------------------------------- repair times
export interface RepairRow {
  label: string;
  scripted: Metric | null;
  deep: Metric | null;
  deepCalls: Metric | null;
}

export function repairRows(r: BenchResults): RepairRow[] {
  const pw = r.patchwork;
  const d = pw.deep_agent;
  return [
    { label: "BUILD (recording → approved v1)", scripted: pw.build?.wall_s ?? null, deep: d?.build_s ?? null, deepCalls: d?.build_calls ?? null },
    { label: "Break 1 repair (incident → approved version)", scripted: pw.breaks?.break1?.repair_s ?? null, deep: d?.repair_break1_s ?? null, deepCalls: d?.repair_break1_calls ?? null },
    { label: "Break 2 repair (incident → approved version)", scripted: pw.breaks?.break2?.repair_s ?? null, deep: d?.repair_break2_s ?? null, deepCalls: d?.repair_break2_calls ?? null },
  ];
}

// ------------------------------------------------------------------------------- formatting
export function fmtSeconds(s: number): string {
  if (s < 0.1) return `${Math.round(s * 1000)} ms`;
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)} s`;
  const m = Math.floor(s / 60);
  const rest = Math.round(s - m * 60);
  return rest ? `${m} min ${rest} s` : `${m} min`;
}

export function fmtMinutes(min: number): string {
  if (min < 1) return `${Math.round(min * 60)} s`;
  if (min < 10) return `${min.toFixed(1)} min`;
  if (min < 120) return `${Math.round(min)} min`;
  return `${(min / 60).toFixed(1)} h`;
}

/** Human-readable value of a metric. "not measured" when there is no value. */
export function fmtMetric(m: Metric | null | undefined): string {
  const v = val(m);
  if (m == null || v == null) return m?.kind === "not measured" || m == null ? "not measured" : "—";
  switch (m.unit) {
    case "s":
      return fmtSeconds(v);
    case "ms":
      return `${v < 1 ? v.toFixed(2) : v < 10 ? v.toFixed(1) : Math.round(v).toLocaleString("en-US")} ms`;
    case "min":
      return fmtMinutes(v);
    case "ratio": {
      const p = v * 100;
      return `${Number.isInteger(p) ? p : p.toFixed(1)}%`;
    }
    case "code":
      return `exit ${v}`;
    default:
      return Math.round(v).toLocaleString("en-US");
  }
}

export interface TableRow {
  group: string;
  label: string;
  text: string;
  metric: Metric;
}

/** Every headline number, grouped, for the compact table. */
export function tableRows(r: BenchResults): TableRow[] {
  const rows: TableRow[] = [];
  const add = (group: string, label: string, metric: Metric | undefined) => {
    if (metric) rows.push({ group, label, text: fmtMetric(metric), metric });
  };
  const pw = r.patchwork;
  const g1 = `Patchwork (${pw.agent} agent, this run)`;
  add(g1, "BUILD: recording → approved v1", pw.build.wall_s);
  add(g1, "BUILD LLM calls", pw.build.llm_calls);
  add(g1, "Run: records", pw.run.records);
  add(g1, "Run: wall time", pw.run.wall_s);
  add(g1, "Run: per record", pw.run.per_record_ms);
  add(g1, "Run: LLM calls", pw.run.llm_calls);
  add(g1, "Run: accuracy vs ground truth", pw.run.accuracy);
  add(g1, "Seed accuracy (all records)", pw.seed_accuracy);
  add(g1, "Human setup time", pw.setup_human_min);

  const d = pw.deep_agent;
  const g2 = `Patchwork deep agent (${d.date})`;
  add(g2, "BUILD", d.build_s);
  add(g2, "BUILD model calls", d.build_calls);
  add(g2, "Break 1 repair", d.repair_break1_s);
  add(g2, "Break 1 model calls", d.repair_break1_calls);
  add(g2, "Break 2 repair", d.repair_break2_s);
  add(g2, "Break 2 model calls", d.repair_break2_calls);
  add(g2, "Whole demo", d.demo_total_s);

  const b = r.baseline_script;
  const g3 = "One-shot script from Claude";
  add(g3, "Run: records", b.run.records);
  add(g3, "Run: wall time", b.run.wall_s);
  add(g3, "Run: per record", b.run.per_record_ms);
  add(g3, "Run: LLM calls", b.run.llm_calls);
  add(g3, "Correct writes", b.run.correct_writes);
  add(g3, "Wrong writes", b.run.wrong_writes);
  add(g3, "Silently dropped", b.run.silently_dropped);
  add(g3, "Held and logged", b.run.held);
  add(g3, "Accuracy vs ground truth", b.run.accuracy);
  add(g3, "Exit code (first run)", b.run.exit_code);
  add(g3, "Exit code (nothing new)", b.steady_state_exit_code);

  add("Manual", "Per record", r.manual.per_record_min);
  add("Computer-use agent", "Per record", r.computer_use.per_record_min);
  add("Computer-use agent", "Setup", r.computer_use.setup_min);
  add("Computer-use agent", "Model calls per record", r.computer_use.llm_calls_per_record);
  return rows;
}

// ------------------------------------------------------------------------------- v2: AI calls over postings
export type AiSeriesId = "patchwork" | "browser_agent";
export interface AiSeries {
  id: AiSeriesId;
  label: string;
  kind: BenchKind;
  style: LineStyle;
  /** cumulative AI calls after posting x, starting at (0, 0) */
  points: { x: number; y: number }[];
  /** postings that cost AI calls (a new layout was learned) */
  learns: { x: number; y: number; site: string; calls: number }[];
}

const kindOf = (r: BenchV2Results, re: RegExp, dflt: BenchKind): BenchKind => r.numbers?.find((n) => re.test(n.name))?.kind ?? dflt;
const cumulative = (xs: number[]) => {
  let a = 0;
  return xs.map((x) => (a += x || 0));
};

/** Both cumulative lines. Patchwork defaults to measured, the browser agent to an estimate, unless a number says otherwise. */
export function aiCallSeries(r: BenchV2Results): AiSeries[] {
  const out: AiSeries[] = [];
  const posts = r.postings ?? [];
  const pwY = r.series?.patchwork_cumulative_ai_calls ?? cumulative(posts.map((p) => p.ai_calls));
  if (pwY.length) {
    const kind = kindOf(r, /patchwork.*ai calls/i, "measured");
    out.push({
      id: "patchwork", label: "Patchwork", kind, style: styleForKind(kind),
      points: [{ x: 0, y: 0 }, ...pwY.map((y, i) => ({ x: i + 1, y }))],
      learns: posts.filter((p) => p.ai_calls > 0).map((p) => ({ x: p.n, y: pwY[p.n - 1] ?? 0, site: p.site, calls: p.ai_calls })),
    });
  }
  const baY = r.series?.browser_agent_cumulative_ai_calls;
  if (baY?.length) {
    const kind = kindOf(r, /browser agent/i, "estimate");
    out.push({ id: "browser_agent", label: "Browser agent", kind, style: styleForKind(kind), points: [{ x: 0, y: 0 }, ...baY.map((y, i) => ({ x: i + 1, y }))], learns: [] });
  }
  return out;
}

export function aiSummary(r: BenchV2Results) {
  const s = aiCallSeries(r);
  const pw = s.find((x) => x.id === "patchwork");
  const ba = s.find((x) => x.id === "browser_agent");
  const postings = r.postings?.length ?? Math.max(0, (pw?.points.length ?? 1) - 1);
  const lastLearn = pw?.learns.at(-1)?.x ?? 0;
  return {
    postings,
    patchwork: pw?.points.at(-1)?.y ?? 0,
    browser: ba?.points.at(-1)?.y ?? null,
    learns: pw?.learns.length ?? 0,
    lastLearn,
    zeroAfter: postings - lastLearn,
  };
}

/** Short badge text for a kind. */
export function kindLabel(kind: BenchKind | string): string {
  if (kind === "measured (earlier run)") return "measured · earlier run";
  return kind;
}
