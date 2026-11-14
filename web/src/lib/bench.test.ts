import { describe, expect, it } from "vitest";
import type { BenchResults, BreakResult, Metric } from "../api";
import {
  breakEven, breakRows, caughtTally, costAt, costModels, fmtMetric, niceTicks, readout, repairRows, segments, tableRows, val,
} from "./bench";

const m = (value: number | null, unit: string, kind: Metric["kind"] = "measured", source = "test"): Metric => ({ value, unit, kind, source });
const brk = (over: Partial<BreakResult>): BreakResult => ({
  name: "Break", records_in_break: 5, detected: true, caught_before_bad_write: true,
  bad_writes: m(0, "records"), silently_dropped: m(0, "records"), held: m(5, "records"),
  time_to_detect_s: m(0.4, "s"), repair_s: m(0.9, "s"), incident_type: "CONTRACT_CHANGED",
  alert: "incident", outcome: "blocked", ...over,
});

function fixture(): BenchResults {
  return {
    schema_version: 1,
    generated_at: "2026-11-14T12:00:00Z",
    command: "python -m bench.run",
    scenario: { seed_records: 150, run_records: 100, break1: "v2", break2: "umbrella", notes: [] },
    patchwork: {
      agent: "scripted",
      build: { wall_s: m(1.2, "s"), llm_calls: m(0, "calls") },
      run: { records: m(100, "records"), wall_s: m(1.2, "s"), per_record_ms: m(12, "ms"), llm_calls: m(0, "calls"), accuracy: m(1, "ratio") },
      seed_accuracy: m(1, "ratio"),
      setup_human_min: m(2, "min", "estimate"),
      breaks: {
        break1: brk({ name: "Break 1: Stockroom contract v2", held: m(5, "records") }),
        break2: brk({ name: "Break 2: new supplier format", records_in_break: 4, held: m(4, "records"), incident_type: "NEW_SOURCE_FORMAT" }),
      },
      deep_agent: {
        date: "2026-11-14", model: "nemotron", source: "smoke-deep2.log",
        build_s: m(138, "s", "measured (earlier run)"), build_calls: m(30, "calls", "measured (earlier run)"),
        repair_break1_s: m(78, "s", "measured (earlier run)"), repair_break1_calls: m(22, "calls", "measured (earlier run)"),
        repair_break2_s: m(104, "s", "measured (earlier run)"), repair_break2_calls: m(27, "calls", "measured (earlier run)"),
        demo_total_s: m(325, "s", "measured (earlier run)"),
      },
    },
    baseline_script: {
      prompt: "bench/baseline_prompt.md", script: "bench/baseline_script.py", how_written: "one shot",
      run: {
        records: m(150, "records"), wall_s: m(2, "s"), per_record_ms: m(13.3, "ms"), llm_calls: m(0, "calls"),
        correct_writes: m(120, "records"), wrong_writes: m(0, "records"), silently_dropped: m(0, "records"),
        held: m(18, "records"), accuracy: m(1, "ratio"), exit_code: m(1, "code"),
      },
      steady_state_exit_code: m(1, "code"),
      breaks: {
        break1: brk({ name: "Break 1", detected: false, caught_before_bad_write: true, time_to_detect_s: m(null, "s", "not measured"), repair_s: m(null, "s", "not measured"), incident_type: null }),
        break2: brk({ name: "Break 2", detected: false, caught_before_bad_write: false, silently_dropped: m(4, "records"), held: m(0, "records"), repair_s: m(null, "s", "not measured"), incident_type: null }),
      },
    },
    manual: { per_record_min: m(1, "min", "estimate"), timings_s: [] },
    computer_use: { per_record_min: m(3, "min", "estimate"), setup_min: m(5, "min", "estimate"), llm_calls_per_record: m(15, "calls", "estimate"), timings_s: [] },
    summary: { breaks_total: 2, caught_before_bad_write: { patchwork: 2, baseline_script: 0 } },
  };
}

describe("cost models", () => {
  it("builds Patchwork setup from the deep agent's BUILD plus the human estimate", () => {
    const pw = costModels(fixture()).find((x) => x.id === "patchwork")!;
    expect(pw.setupMin).toBeCloseTo(138 / 60 + 2);
    expect(pw.setupParts.map((p) => p.kind)).toEqual(["measured (earlier run)", "estimate"]);
    expect(pw.perRecordMin).toBeCloseTo(12 / 60000);
    // repairs land after the seed (Break 1) and after the Break 1 deliveries (Break 2)
    expect(pw.steps.map((s) => [s.x, +s.minutes.toFixed(3)])).toEqual([[150, 1.3], [155, +(104 / 60).toFixed(3)]]);
    expect(costAt(pw, 0)).toBeCloseTo(pw.setupMin);
    expect(costAt(pw, 151) - costAt(pw, 150)).toBeCloseTo(1.3 + 12 / 60000);
  });

  it("falls back to the scripted BUILD time without a deep-agent number", () => {
    const r = fixture();
    r.patchwork.deep_agent.build_s = m(null, "s", "not measured");
    const pw = costModels(r).find((x) => x.id === "patchwork")!;
    expect(pw.setupParts[0].label).toBe("BUILD (scripted agent)");
    expect(pw.setupMin).toBeCloseTo(1.2 / 60 + 2);
  });

  it("leaves out a system that was not measured or estimated", () => {
    const r = fixture();
    r.computer_use.per_record_min = m(null, "min", "not measured");
    expect(costModels(r).map((x) => x.id)).toEqual(["manual", "patchwork"]);
  });

  it("tags Patchwork measured up to the run, extrapolated after; estimates stay estimates", () => {
    const [manual, , pw] = costModels(fixture());
    expect(new Set(segments(manual, 200).map((s) => s.style))).toEqual(new Set(["estimate"]));
    const segs = segments(pw, 200);
    expect(segs[0]).toMatchObject({ x0: 0, x1: 100, style: "measured" });
    expect(segs.filter((s) => !s.jump && s.x0 >= 100).every((s) => s.style === "extrapolated")).toBe(true);
    const jumps = segs.filter((s) => s.jump);
    expect(jumps.map((s) => [s.x0, s.jump, s.style])).toEqual([[150, "Break 1 repair", "measured"], [155, "Break 2 repair", "measured"]]);
    // continuous: every segment starts where the previous one ended
    for (let i = 1; i < segs.length; i++) {
      expect(segs[i].x0).toBeCloseTo(segs[i - 1].x1);
      expect(segs[i].y0).toBeCloseTo(segs[i - 1].y1);
    }
  });

  it("measured manual timings make the manual line measured for those records, extrapolated after", () => {
    const r = fixture();
    r.manual = { per_record_min: m(0.8, "min", "measured"), timings_s: [50, 46, 49, 47, 48] };
    const manual = costModels(r)[0];
    expect(manual.measuredUpTo).toBe(5);
    expect(segments(manual, 20).map((s) => s.style)).toEqual(["measured", "extrapolated"]);
  });

  it("finds the break-even record count", () => {
    const [manual, cu, pw] = costModels(fixture());
    // 4.3 min of setup vs 1 min per record by hand
    expect(breakEven(pw, manual)).toBe(5);
    expect(breakEven(pw, cu)).toBe(1);
    expect(breakEven(manual, pw, 3)).toBe(1);
    expect(breakEven(pw, undefined)).toBeNull();
  });

  it("reads every series at one x", () => {
    const rows = readout(costModels(fixture()), 10);
    expect(rows.map((r) => [r.id, +r.minutes.toFixed(2), r.style])).toEqual([
      ["manual", 10, "estimate"], ["computer_use", 35, "estimate"], ["patchwork", +(4.3 + 0.002).toFixed(2), "measured"],
    ]);
  });
});

describe("breaks and repairs", () => {
  it("counts breaks caught before any bad write", () => {
    expect(caughtTally(fixture())).toEqual({ total: 2, patchwork: 2, baseline_script: 0 });
  });

  it("pairs Patchwork and the baseline per break", () => {
    const rows = breakRows(fixture());
    expect(rows.map((r) => [r.key, r.name, r.records, r.baseline.silently_dropped.value])).toEqual([
      ["break1", "Break 1: Stockroom contract v2", 5, 0],
      ["break2", "Break 2: new supplier format", 4, 4],
    ]);
  });

  it("lists scripted and deep repair times side by side", () => {
    const rows = repairRows(fixture());
    expect(rows.map((r) => [fmtMetric(r.scripted), fmtMetric(r.deep), fmtMetric(r.deepCalls)])).toEqual([
      ["1.2 s", "2 min 18 s", "30"], ["0.9 s", "1 min 18 s", "22"], ["0.9 s", "1 min 44 s", "27"],
    ]);
  });
});

describe("formatting and table", () => {
  it("formats by unit and handles missing values", () => {
    expect(fmtMetric(m(12.4, "ms"))).toBe("12 ms");
    expect(fmtMetric(m(0.5, "ms"))).toBe("0.50 ms");
    expect(fmtMetric(m(1, "ratio"))).toBe("100%");
    expect(fmtMetric(m(0.9733, "ratio"))).toBe("97.3%");
    expect(fmtMetric(m(1, "min", "estimate"))).toBe("1.0 min");
    expect(fmtMetric(m(0.5, "min"))).toBe("30 s");
    expect(fmtMetric(m(1, "code"))).toBe("exit 1");
    expect(fmtMetric(m(null, "s", "not measured"))).toBe("not measured");
    expect(fmtMetric(undefined)).toBe("not measured");
    expect(val(m(null, "s"))).toBeNull();
  });

  it("keeps every number's kind in the table", () => {
    const rows = tableRows(fixture());
    expect(rows.length).toBeGreaterThan(25);
    expect(rows.every((r) => r.metric.kind && r.text)).toBe(true);
    const human = rows.find((r) => r.label === "Human setup time")!;
    expect([human.text, human.metric.kind]).toEqual(["2.0 min", "estimate"]);
    expect(rows.find((r) => r.group.startsWith("Patchwork deep agent") && r.label === "BUILD")!.metric.kind).toBe("measured (earlier run)");
  });

  it("makes clean axis ticks", () => {
    expect(niceTicks(600)).toEqual([0, 200, 400, 600]);
    expect(niceTicks(23)).toEqual([0, 5, 10, 15, 20, 25]);
    expect(niceTicks(0)).toEqual([0, 1]);
  });
});
