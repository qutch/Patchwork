import { describe, expect, it } from "vitest";
import type { Layout, Settings, TrackerRow } from "../api";
import { aiCalls, heldHint, labelKind, labelText, layoutRows, localDay, originText, totals } from "./tracker";

const row = (over: Partial<TrackerRow> = {}): TrackerRow => ({
  page_id: "pg-000001", seq: 1, captured_at: "2026-11-14T14:02:11+00:00", site: "jobs.lever.co", url: "https://jobs.lever.co/x/1",
  title: "American Tech Fellowship", company: "Palantir Technologies", status: "success", label: "code", reason: null, detail: null,
  parser: "lever", version: 3, routed_by: "match", parser_origin: { kind: "taught", at: null, thread_id: null, approved_by: "human" },
  ai_calls: 0, run_id: 7, learn_thread_id: null, incident_id: null, ...over,
});
const deep: Settings = { ai_enabled: true, agent: "deep", model: "m", learn_policy: "auto" };
const scripted: Settings = { ai_enabled: true, agent: "scripted", model: "scripted", learn_policy: "auto" };
const learned = row({ label: "learned_now", ai_calls: 5, learn_thread_id: "learn-1", parser: "ashby", parser_origin: { kind: "learned", at: "2026-11-14T14:02:11+00:00", thread_id: "learn-1", approved_by: "auto" } });

describe("labelText", () => {
  it("says a code row used no AI", () => {
    expect(labelText(row(), deep)).toBe("code · 0 AI calls");
    expect(labelText(row(), scripted)).toBe("code · 0 AI calls");
  });
  it("says a learned row used AI, how many calls, and that code checked it", () => {
    expect(labelText(learned, deep)).toBe("learned now · 5 AI calls (checked by code)");
    expect(labelText({ ...learned, ai_calls: 1 }, deep)).toBe("learned now · 1 AI call (checked by code)");
  });
  it("names the scripted agent instead of implying a model ran", () => {
    const t = labelText({ ...learned, ai_calls: 0 }, scripted);
    expect(t).toBe("learned now · scripted agent, 0 AI calls (checked by code)");
    expect(labelText({ ...row(), status: "learning", label: null }, scripted)).toBe("learning this layout (scripted agent)…");
    expect(labelText({ ...row(), status: "learning", label: null }, deep)).toBe("learning this layout with AI…");
  });
  it("says why a row is held", () => {
    expect(labelText(row({ status: "held", label: "held", reason: "PARSE_NO_MATCH" }), deep)).toBe("held: unknown layout");
    expect(labelText(row({ status: "held", label: "held", reason: "CONTRACT_CHANGED", incident_id: 4 }), deep)).toBe("held: form changed");
    expect(labelText(row({ status: "held", label: "held", reason: "SINK_CONTRACT" }), deep)).toBe("held: form changed");
    expect(labelText(row({ status: "held", label: "held", reason: "VALIDATION_FAILED" }), deep)).toBe("held: title or company missing");
    expect(labelText(row({ status: "held", label: "held", reason: "VERIFY_MISMATCH" }), deep)).toBe("held: form did not confirm");
  });
  it("duplicates, in-flight and failed rows", () => {
    expect(labelText(row({ status: "duplicate", label: "duplicate" }), deep)).toBe("already in tracker");
    expect(labelText(row({ status: "pending", label: null }), deep)).toBe("sending…");
    expect(labelText(row({ status: "running", label: null }), deep)).toBe("writing to the form…");
    expect(labelText(row({ status: "failed", label: null, reason: "UNKNOWN" }), deep)).toBe("failed: UNKNOWN");
  });
  it("works before settings have loaded", () => {
    expect(labelText(learned, null)).toBe("learned now · 5 AI calls (checked by code)");
  });
});

describe("labelKind", () => {
  it("maps rows to one chip style", () => {
    expect(labelKind(row())).toBe("code");
    expect(labelKind(learned)).toBe("learned");
    expect(labelKind(row({ status: "held", label: "held" }))).toBe("held");
    expect(labelKind(row({ status: "duplicate", label: "duplicate" }))).toBe("duplicate");
    expect(labelKind(row({ status: "learning", label: null }))).toBe("learning");
    expect(labelKind(row({ status: "running", label: null }))).toBe("pending");
    expect(labelKind(row({ status: "failed", label: null }))).toBe("failed");
  });
});

describe("heldHint", () => {
  const held = (reason: string, over: Partial<TrackerRow> = {}) => row({ status: "held", label: "held", reason, ...over });
  it("tells a person what they can do", () => {
    expect(heldHint(held("PARSE_NO_MATCH"), { ...deep, ai_enabled: false })).toMatch(/AI is off.*Turn AI on.*replay/i);
    expect(heldHint(held("PARSE_NO_MATCH"), deep)).toMatch(/Replay held pages/);
    expect(heldHint(held("PARSE_NO_MATCH", { learn_thread_id: "learn-9" }), deep)).toMatch(/approve/i);
    expect(heldHint(held("CONTRACT_CHANGED", { incident_id: 3 }), deep)).toMatch(/nothing was written.*Repair with AI/i);
    expect(heldHint(held("VALIDATION_FAILED"), deep)).toMatch(/title or company/);
    expect(heldHint(row(), deep)).toBeNull();
  });
});

describe("totals", () => {
  it("counts labels and AI calls, optionally for one local day", () => {
    const rows = [
      row({ seq: 5, captured_at: "2026-11-14T15:00:00+00:00" }),
      { ...learned, seq: 4, captured_at: "2026-11-14T14:00:00+00:00" },
      row({ seq: 3, status: "held", label: "held", reason: "PARSE_NO_MATCH" }),
      row({ seq: 2, status: "duplicate", label: "duplicate" }),
      { ...learned, seq: 1, ai_calls: 7, captured_at: "2026-11-10T09:00:00+00:00" },
    ];
    expect(totals(rows)).toEqual({ pages: 5, ai_calls: 12, code: 1, learned_now: 2, held: 1, duplicate: 1 });
    const day = localDay("2026-11-14T15:00:00+00:00");
    expect(totals(rows, day).ai_calls).toBe(5);
    expect(totals(rows, day).pages).toBe(4);
    expect(totals([])).toEqual({ pages: 0, ai_calls: 0, code: 0, learned_now: 0, held: 0, duplicate: 0 });
  });
  it("aiCalls pluralises", () => {
    expect(aiCalls(0)).toBe("0 AI calls");
    expect(aiCalls(1)).toBe("1 AI call");
  });
});

describe("layouts", () => {
  const lay = (parser_id: string, origin: Layout["origin"], active = true, introduced_in = 1): Layout => ({
    integration: "jobs_to_tracker", parser_id, site: `${parser_id}.example`, fingerprint: null, fields: {}, origin, introduced_in, active,
  });
  it("orders active first, newest learned first, then by id", () => {
    const rows = layoutRows([
      lay("lever", { kind: "taught", at: "2026-11-14T09:00:00+00:00", thread_id: null, approved_by: "human" }),
      lay("old", { kind: "learned", at: "2026-11-14T15:00:00+00:00", thread_id: "t", approved_by: "auto" }, false),
      lay("ashby", { kind: "learned", at: "2026-11-14T11:00:00+00:00", thread_id: "t2", approved_by: "auto" }),
      lay("greenhouse", { kind: "learned", at: "2026-11-14T10:00:00+00:00", thread_id: "t3", approved_by: "human" }),
      lay("fixture_b", null),
      lay("fixture_a", { kind: "fixture", at: null, thread_id: null, approved_by: null }),
    ]);
    expect(rows.map((l) => l.parser_id)).toEqual(["ashby", "greenhouse", "lever", "fixture_a", "fixture_b", "old"]);
  });
  it("words each origin honestly", () => {
    expect(originText({ kind: "learned", at: null, thread_id: "t", approved_by: "auto" })).toEqual({
      how: "learned by AI, checked by code", approval: "auto-approved: every code check passed",
    });
    expect(originText({ kind: "learned", at: null, thread_id: "t", approved_by: "human" }).approval).toBe("approved by a person");
    expect(originText({ kind: "learned", at: null, thread_id: "t", approved_by: null }).approval).toBe("waiting for a person");
    expect(originText({ kind: "taught", at: null, thread_id: null, approved_by: "human" })).toEqual({ how: "taught from one recording", approval: "approved by a person" });
    expect(originText({ kind: "fixture", at: null, thread_id: null, approved_by: null })).toEqual({ how: "built in (test fixture)", approval: "—" });
    expect(originText(null)).toEqual({ how: "unknown origin", approval: "—" });
  });
});
