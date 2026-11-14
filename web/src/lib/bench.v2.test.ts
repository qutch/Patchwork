// bench v2 (V12): AI calls over postings.
import { describe, expect, it } from "vitest";
import type { BenchV2Results } from "../api";
import { aiCallSeries, aiSummary } from "./bench";

const posting = (n: number, ai_calls: number, site = "jobs.lever.co") => ({ n, file: `f/${n}`, site, label: ai_calls ? "learned_now" : "code", ai_calls, seconds: 1, correct: true });
const v2 = (over: Partial<BenchV2Results> = {}): BenchV2Results => ({
  postings: [posting(1, 6), posting(2, 0), posting(3, 5, "jobs.ashbyhq.com"), posting(4, 0)],
  series: { patchwork_cumulative_ai_calls: [6, 6, 11, 11], browser_agent_cumulative_ai_calls: [4, 8, 12, 16] },
  numbers: [
    { name: "Patchwork AI calls, total", value: 11, unit: "calls", kind: "measured", source: "bench/v2" },
    { name: "Browser agent AI calls per posting", value: 4, unit: "calls", kind: "estimate", source: "assumption" },
  ],
  ...over,
});

describe("aiCallSeries", () => {
  it("starts both lines at 0 and labels measured solid, estimate dashed", () => {
    const [pw, ba] = aiCallSeries(v2());
    expect(pw.id).toBe("patchwork");
    expect(pw.kind).toBe("measured");
    expect(pw.style).toBe("measured");
    expect(pw.points).toEqual([{ x: 0, y: 0 }, { x: 1, y: 6 }, { x: 2, y: 6 }, { x: 3, y: 11 }, { x: 4, y: 11 }]);
    expect(pw.learns).toEqual([{ x: 1, y: 6, site: "jobs.lever.co", calls: 6 }, { x: 3, y: 11, site: "jobs.ashbyhq.com", calls: 5 }]);
    expect(ba.id).toBe("browser_agent");
    expect(ba.kind).toBe("estimate");
    expect(ba.style).toBe("estimate");
    expect(ba.points.at(-1)).toEqual({ x: 4, y: 16 });
  });

  it("derives Patchwork's cumulative series from postings when the series is missing", () => {
    const [pw] = aiCallSeries(v2({ series: { browser_agent_cumulative_ai_calls: [4, 8, 12, 16] } }));
    expect(pw.points.map((p) => p.y)).toEqual([0, 6, 6, 11, 11]);
  });

  it("takes a series' kind from its number, and defaults honestly without one", () => {
    const est = aiCallSeries(v2({ numbers: [{ name: "Patchwork AI calls, total", value: 11, unit: "calls", kind: "extrapolated", source: "x" }] }));
    expect(est[0].kind).toBe("extrapolated");
    expect(est[0].style).toBe("extrapolated");
    const s = aiCallSeries(v2({ series: { patchwork_cumulative_ai_calls: [6, 6, 11, 11] }, numbers: [] }));
    expect(s.map((x) => x.id)).toEqual(["patchwork"]);
    expect(aiCallSeries(v2({ numbers: [] })).map((x) => x.kind)).toEqual(["measured", "estimate"]);
  });

  it("summarises where Patchwork stopped needing AI", () => {
    expect(aiSummary(v2())).toEqual({ postings: 4, patchwork: 11, browser: 16, learns: 2, lastLearn: 3, zeroAfter: 1 });
  });
});
