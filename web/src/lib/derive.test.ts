import { describe, expect, it } from "vitest";
import type { AgentEvent, CaptureEvent } from "../api";
import data from "../../mock/data.json";
import { deriveThread, describeEvent, diffStats, mergeEvents, parseUnifiedDiff, segments, splitFileDiffs } from "./derive";

// Real events from ScriptedAgentService (web/mock/collect.py).
const build = data.build_events as unknown as AgentEvent[];
const repair = data.repair.by_supplier.events as unknown as AgentEvent[];

describe("deriveThread", () => {
  it("is paused on ask_user right after the interpreter finishes", () => {
    const upto = build.findIndex((e) => e.type === "ask_user");
    const v = deriveThread(build.slice(0, upto + 2)); // + the "waiting" status
    expect(v.state).toBe("waiting");
    expect(v.pending?.type).toBe("ask_user");
    expect(v.todos.map((t) => t.status)).toEqual(["completed", "completed", "in_progress", "pending", "pending"]);
    expect(v.subagents).toHaveLength(1);
    expect(v.subagents[0]).toMatchObject({ name: "interpreter", tools: 7, failedTools: 1 });
    expect(v.subagents[0].endT).not.toBeNull();
  });

  it("pairs every tool_call with its result and records answers on gates", () => {
    const v = deriveThread(build);
    expect(v.state).toBe("done");
    expect(v.pending).toBeNull();
    const tools = v.feed.filter((f) => f.kind === "tool");
    expect(tools.length).toBe(build.filter((e) => e.type === "tool_call").length);
    expect(tools.every((t) => t.kind === "tool" && t.result)).toBe(true);
    const gates = v.feed.filter((f) => f.kind === "gate");
    expect(gates.map((g) => g.kind === "gate" && g.answer)).toEqual([
      { answer: "Quarantine them for review" },
      { decision: "approve", comment: "" },
    ]);
    expect(v.subagents.map((s) => s.name)).toEqual(["interpreter", "builder", "tester"]);
  });

  it("shows the repair deploy request with its JSON Patch", () => {
    const upto = repair.findIndex((e) => e.type === "request_deploy");
    const v = deriveThread(repair.slice(0, upto + 1));
    expect(v.mode).toBe("repair");
    expect(v.pending?.type).toBe("request_deploy");
    if (v.pending?.type === "request_deploy") {
      expect(v.pending.version).toBe(2);
      expect(v.pending.spec_patch?.length).toBe(5);
      expect(v.pending.tests.passed).toBe(true);
    }
  });

  it("mergeEvents dedupes and sorts by seq", () => {
    const a = build.slice(0, 3);
    expect(mergeEvents(a, [build[2], build[4], build[3]]).map((e) => e.seq)).toEqual([1, 2, 3, 4, 5]);
    expect(mergeEvents(a, [build[1]])).toBe(a);
  });
});

describe("diffs", () => {
  const d = data.repair.by_supplier.diff;
  it("classifies unified diff lines", () => {
    const kinds = new Set(parseUnifiedDiff(d.spec_diff).map((l) => l.kind));
    expect([...kinds].sort()).toEqual(["add", "ctx", "del", "file", "hunk"]);
    expect(diffStats(d.spec_diff)).toEqual({ add: 14, del: 6 });
  });
  it("treats non-diff text as plain context", () => {
    expect(parseUnifiedDiff("spec_version: 1\nname: x").every((l) => l.kind === "ctx")).toBe(true);
  });
  it("splits concatenated per-file diffs, even when a file has no trailing newline", () => {
    const joined = Object.values(d.code_diff).join("");
    expect(Object.keys(splitFileDiffs(joined))).toEqual(Object.keys(d.code_diff).map((f) => `v2/${f}`));
  });
});

describe("segments and events", () => {
  it("cuts text around highlight spans", () => {
    const body = data.candidates.source.record.body;
    const spans = data.candidates.mappings.map((m) => ({ start: m.offset[0], end: m.offset[1], key: m.dest_field }));
    const segs = segments(body, spans);
    expect(segs.filter((s) => s.key).map((s) => [s.key, s.text])).toEqual([
      ["reference", "PO-1022"],
      ["product_id", "GAD-217"],
      ["quantity", "20"],
    ]);
    expect(segs.map((s) => s.text).join("")).toBe(body);
  });
  it("describes captured events for the feed", () => {
    const ev = data.recording.events as unknown as CaptureEvent[];
    expect(describeEvent(ev.find((e) => e.type === "paste")!)).toMatchObject({ kind: "paste", text: "Reference = “PO-1022”" });
    expect(describeEvent(ev.at(-1)!)).toMatchObject({ kind: "POST", text: "/api/incoming → 201" });
  });
});
