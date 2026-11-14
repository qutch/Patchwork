// @vitest-environment node
// The v2 client calls (contracts V9) against the real mock control plane (web/mock/server.mjs),
// started on a free port with no pacing.
import { spawn, type ChildProcess } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { api, ApiError, setApiBase, type TrackerRow } from "./api";

const HERE = dirname(fileURLToPath(import.meta.url));
let proc: ChildProcess;

beforeAll(async () => {
  proc = spawn(process.execPath, [join(HERE, "..", "mock", "server.mjs"), "--port", "0", "--pace", "0"], { stdio: ["ignore", "pipe", "inherit"] });
  const port = await new Promise<number>((resolve, reject) => {
    const t = setTimeout(() => reject(new Error("mock did not start")), 5000);
    proc.stdout!.on("data", (b: Buffer) => {
      const m = String(b).match(/127\.0\.0\.1:(\d+)/);
      if (m) {
        clearTimeout(t);
        resolve(Number(m[1]));
      }
    });
  });
  setApiBase(`http://127.0.0.1:${port}`);
});
afterAll(() => {
  proc?.kill();
  setApiBase("");
});

const until = async <T>(fn: () => Promise<T | null | undefined | false>, what: string): Promise<T> => {
  for (let i = 0; i < 200; i++) {
    const v = await fn();
    if (v) return v;
    await new Promise((r) => setTimeout(r, 20));
  }
  throw new Error(`timeout: ${what}`);
};
const rowFor = async (pageId: string, ok: (r: TrackerRow) => boolean) =>
  until(async () => {
    const { rows } = await api.tracker();
    const r = rows.find((x) => x.page_id === pageId);
    return r && ok(r) ? r : null;
  }, `row ${pageId}`);
const page = (url: string, title: string) => ({ url, title, html: `<html><head><title>${title}</title></head><body><h1>${title}</h1></body></html>` });

describe("v2 client against the mock", () => {
  it("seeded tracker has a row for every label, newest first, with totals", async () => {
    const t = await api.tracker();
    const labels = new Set(t.rows.map((r) => r.label));
    expect([...labels].sort()).toEqual(["code", "duplicate", "held", "learned_now"]);
    const reasons = t.rows.filter((r) => r.label === "held").map((r) => r.reason).sort();
    expect(reasons).toEqual(["CONTRACT_CHANGED", "PARSE_NO_MATCH"]);
    expect(t.rows.map((r) => r.seq)).toEqual([...t.rows.map((r) => r.seq)].sort((a, b) => b - a));
    expect(t.totals.pages).toBe(t.rows.length);
    expect(t.totals.code).toBe(t.rows.filter((r) => r.label === "code").length);
    expect(t.totals.ai_calls).toBe(t.rows.reduce((a, r) => a + r.ai_calls, 0));
  });

  it("the learned row's thread has evidence and learned events", async () => {
    const { rows } = await api.tracker();
    const learned = rows.find((r) => r.label === "learned_now")!;
    expect(learned.learn_thread_id).toMatch(/^learn-/);
    expect(learned.parser_origin?.thread_id).toBe(learned.learn_thread_id);
    const th = await api.thread(learned.learn_thread_id!);
    expect(th.mode).toBe("learn");
    expect(th.state).toBe("done");
  });

  it("layouts carry auto- and human-approved origins", async () => {
    const layouts = await api.layouts();
    const by = layouts.map((l) => l.origin?.approved_by ?? null);
    expect(by).toContain("auto");
    expect(by).toContain("human");
    expect(layouts.every((l) => l.integration === "jobs_to_tracker")).toBe(true);
  });

  it("extension setup values, and POST /api/pages needs its token", async () => {
    const ext = await api.extension();
    expect(ext.token).toBeTruthy();
    expect(ext.control_url).toMatch(/^http/);
    const bad = await api.postPage(page("https://jobs.lever.co/x/1", "x"), "wrong").catch((e) => e);
    expect(bad).toBeInstanceOf(ApiError);
    expect(bad.status).toBe(401);
  });

  it("a known layout is written by code; the same posting with a new query is a duplicate", async () => {
    await api.v2Reset();
    expect((await api.tracker()).rows).toEqual([]);
    const { token } = await api.extension();
    const a = await api.postPage(page("https://jobs.lever.co/palantir/abc?lever-source=x", "Fellow"), token);
    expect(a.id).toMatch(/^pg-\d{6}$/);
    expect(a.canonical_url).toBe("https://jobs.lever.co/palantir/abc");
    expect(a.integration).toBe("jobs_to_tracker");
    const ra = await rowFor(a.id, (r) => r.status === "success");
    expect(ra.label).toBe("code");
    expect(ra.ai_calls).toBe(0);
    const b = await api.postPage(page("https://jobs.lever.co/palantir/abc#apply", "Fellow"), token);
    const rb = await rowFor(b.id, (r) => r.status !== "pending" && r.status !== "running");
    expect(rb.label).toBe("duplicate");
    const detail = await api.page(b.id);
    expect(detail.page.id).toBe(b.id);
    expect(detail.page.html).toBeUndefined();
    expect(detail.row.label).toBe("duplicate");
    const pages = await api.pages({ since: a.seq });
    expect(pages.map((p) => p.id)).toEqual([b.id]);
  });

  it("an unknown layout is held with AI off, and learned once AI is on", async () => {
    await api.v2Reset();
    const { token } = await api.extension();
    await api.putSettings({ ai_enabled: false });
    const p = await api.postPage(page("https://apply.workable.com/acme/j/1/", "Analyst"), token);
    const held = await rowFor(p.id, (r) => r.status === "held");
    expect(held.reason).toBe("PARSE_NO_MATCH");
    expect(held.learn_thread_id).toBeNull();
    await api.putSettings({ ai_enabled: true });
    await api.replay("jobs_to_tracker");
    const learned = await rowFor(p.id, (r) => r.status === "success");
    expect(learned.label).toBe("learned_now");
    expect(learned.learn_thread_id).toMatch(/^learn-/);
    expect((await api.layouts()).some((l) => l.site === "apply.workable.com" && l.origin?.approved_by === "auto")).toBe(true);
  });

  it("a renamed form question holds the page before any write and opens an incident", async () => {
    await api.v2Reset();
    const { token } = await api.extension();
    await api.formRename("Company", "Employer");
    const p = await api.postPage(page("https://jobs.lever.co/palantir/def", "Engineer"), token);
    const held = await rowFor(p.id, (r) => r.status === "held");
    expect(held.reason).toBe("CONTRACT_CHANGED");
    expect(held.incident_id).not.toBeNull();
    const inc = await api.incident(held.incident_id!);
    expect(inc.integration).toBe("jobs_to_tracker");
    expect(inc.type).toBe("CONTRACT_CHANGED");
    expect(inc.detail?.contract_errors?.join(" ")).toMatch(/Company/);
  });

  it("learn_policy round-trips through settings", async () => {
    expect((await api.putSettings({ learn_policy: "approve" })).learn_policy).toBe("approve");
    expect((await api.settings()).learn_policy).toBe("approve");
    await api.putSettings({ learn_policy: "auto" });
  });

  it("reverting a learned layout deploys a new version without it", async () => {
    await api.v2Reset();
    const before = await api.layouts();
    const learned = before.find((l) => l.active && l.origin?.kind === "learned")!;
    const r = await api.revertLayout(learned.parser_id);
    expect(r.deployed).toBe(true);
    const after = await api.layouts();
    expect(after.find((l) => l.parser_id === learned.parser_id)?.active).toBe(false);
  });

  it("bench v2 results have both cumulative series and labelled numbers", async () => {
    const b = await api.benchV2();
    expect(b.series.patchwork_cumulative_ai_calls?.length).toBe(b.postings.length);
    expect(b.series.browser_agent_cumulative_ai_calls?.length).toBe(b.postings.length);
    expect(b.numbers.some((n) => n.kind === "estimate")).toBe(true);
    expect(b.numbers.some((n) => n.kind === "measured")).toBe(true);
  });
});
