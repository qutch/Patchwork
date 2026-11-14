import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, finishedSummary, openStream, setApiBase, splitCounts, withAfter } from "./api";

/** Minimal EventSource stand-in: tests push named events and errors by hand. */
class FakeES {
  static instances: FakeES[] = [];
  readyState = 0;
  listeners = new Map<string, ((e: MessageEvent) => void)[]>();
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  constructor(public url: string) {
    FakeES.instances.push(this);
  }
  addEventListener(name: string, fn: (e: MessageEvent) => void) {
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), fn]);
  }
  close() {
    this.closed = true;
    this.readyState = 2;
  }
  emit(name: string, id: number | null, data: unknown) {
    const ev = { type: name, data: JSON.stringify(data), lastEventId: id == null ? "" : String(id) } as MessageEvent;
    for (const fn of this.listeners.get(name) ?? []) fn(ev);
  }
  drop() {
    this.readyState = 2;
    this.onerror?.();
  }
}

afterEach(() => {
  FakeES.instances = [];
  vi.useRealTimers();
  vi.unstubAllGlobals();
  setApiBase("");
});

describe("openStream", () => {
  it("delivers named events, skips repeats and reconnects with ?after=<last id>", () => {
    vi.useFakeTimers();
    const got: number[] = [];
    openStream<{ n: number }>("/api/agent/t1/stream", {
      events: ["status", "todos"],
      onMessage: (m) => got.push(m.data.n),
      EventSourceImpl: FakeES as unknown as typeof EventSource,
    });
    const first = FakeES.instances[0];
    expect(first.url).toBe("/api/agent/t1/stream");
    first.emit("status", 1, { n: 1 });
    first.emit("todos", 2, { n: 2 });
    first.emit("todos", 2, { n: 2 }); // replayed after a native reconnect
    first.drop();
    vi.advanceTimersByTime(300);
    const second = FakeES.instances[1];
    expect(second.url).toBe("/api/agent/t1/stream?after=2");
    second.emit("status", 3, { n: 3 });
    expect(got).toEqual([1, 2, 3]);
  });

  it("closes for good on a terminal event and does not reconnect", () => {
    vi.useFakeTimers();
    const states: string[] = [];
    const h = openStream<{ event?: string }>("/api/runs/4/stream", {
      events: ["progress", "finished"],
      after: 7,
      onMessage: () => {},
      isTerminal: (m) => m.event === "finished",
      onStateChange: (s) => states.push(s),
      EventSourceImpl: FakeES as unknown as typeof EventSource,
    });
    const es = FakeES.instances[0];
    expect(es.url).toBe("/api/runs/4/stream?after=7");
    es.emit("progress", 8, {});
    es.emit("finished", 9, {});
    expect(es.closed).toBe(true);
    expect(h.lastId()).toBe(9);
    vi.advanceTimersByTime(10_000);
    expect(FakeES.instances).toHaveLength(1);
    expect(states.at(-1)).toBe("closed");
  });

  it("snapshot streams (runs) keep events whose id repeats", () => {
    const got: unknown[] = [];
    openStream<{ processed: number; total: number | null }>("/api/runs/1/stream", {
      events: ["progress", "finished"],
      snapshots: true,
      onMessage: (m) => got.push(m.data.total),
      EventSourceImpl: FakeES as unknown as typeof EventSource,
    });
    const es = FakeES.instances[0];
    es.emit("progress", 0, { processed: 0, total: null }); // id = processed count
    es.emit("progress", 0, { processed: 0, total: 100 }); // total became known
    es.emit("finished", 1, { processed: 0, total: 100 });
    expect(got).toEqual([null, 100, 100]);
  });

  it("withAfter keeps an existing query", () => {
    expect(withAfter("/x?a=1", 5)).toBe("/x?a=1&after=5");
    expect(withAfter("/x", 0)).toBe("/x");
  });
});

describe("request", () => {
  it("sends JSON and surfaces {detail} errors as ApiError", async () => {
    const calls: [string, RequestInit][] = [];
    vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
      calls.push([url, init]);
      if (url.endsWith("/build")) return new Response(JSON.stringify({ detail: "AI is off" }), { status: 409 });
      return new Response(JSON.stringify({ run_id: 3 }), { status: 200 });
    });
    setApiBase("http://cp");
    await expect(api.startRun({ integration: "po_to_stockroom", limit: 100 })).resolves.toEqual({ run_id: 3 });
    expect(calls[0][0]).toBe("http://cp/api/runs");
    expect(JSON.parse(String(calls[0][1].body))).toEqual({ integration: "po_to_stockroom", limit: 100 });
    const err = await api.build("rec-1").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(409);
    expect(err.detail).toBe("AI is off");
  });

  it("builds query strings for diff and runs", async () => {
    const urls: string[] = [];
    vi.stubGlobal("fetch", async (url: string) => {
      urls.push(url);
      return new Response("[]", { status: 200 });
    });
    await api.diff("po_to_stockroom", 1, 2);
    await api.runs({ integration: "po_to_stockroom" });
    await api.incidents("open");
    expect(urls).toEqual([
      "/api/integrations/po_to_stockroom/diff?from=1&to=2",
      "/api/runs?integration=po_to_stockroom",
      "/api/incidents?status=open",
    ]);
  });
});

describe("helpers", () => {
  it("splitCounts reads the runner's count keys", () => {
    const c = splitCounts({ success: 82, duplicate: 3, quarantined: 15, "quarantined:NOT_FOUND": 5, "quarantined:VALIDATION_FAILED": 8, "quarantined:PARSE_NO_MATCH": 2 });
    expect(c).toEqual({ success: 82, duplicate: 3, quarantined: 15, reasons: { NOT_FOUND: 5, VALIDATION_FAILED: 8, PARSE_NO_MATCH: 2 } });
  });
  it("finishedSummary accepts {summary} or a bare summary", () => {
    const s = { run_id: 1, status: "completed" } as never;
    expect(finishedSummary({ summary: s })).toBe(s);
    expect(finishedSummary(s)).toBe(s);
  });
});
