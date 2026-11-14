// End-to-end smoke test of the mock: the full demo script over HTTP, no browser.
//   node mock/smoke.mjs            (starts its own mock with --pace 0 on a free port)
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const PORT = 5199;
const BASE = `http://127.0.0.1:${PORT}`;
const proc = spawn(process.execPath, [join(dirname(fileURLToPath(import.meta.url)), "server.mjs"), "--port", String(PORT), "--pace", "0.02"], { stdio: "inherit" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const j = async (method, path, body) => {
  const r = await fetch(BASE + path, { method, headers: { "content-type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  const d = await r.json();
  if (!r.ok) throw Object.assign(new Error(`${method} ${path}: ${r.status} ${d.detail}`), { status: r.status });
  return d;
};
const until = async (fn, what) => {
  for (let i = 0; i < 400; i++) {
    const v = await fn();
    if (v) return v;
    await sleep(25);
  }
  throw new Error(`timeout: ${what}`);
};
const waitPending = (tid, type) => until(async () => { const s = await j("GET", `/api/agent/${tid}`); return s.pending?.type === type && s.pending; }, type);
const waitState = (tid, state) => until(async () => (await j("GET", `/api/agent/${tid}`)).state === state, state);
const waitRun = (id) => until(async () => { const d = await j("GET", `/api/runs/${id}`); return d.run.status !== "running" && d; }, `run ${id}`);

try {
  await until(() => fetch(BASE + "/api/health").then((r) => r.ok, () => false), "server");
  const { id } = await j("POST", "/api/recordings", { intent: "x", allowlist: ["localhost:8001"] });
  await until(async () => (await j("GET", `/api/recordings/${id}`)).events.length === 12, "12 events");
  await j("POST", `/api/recordings/${id}/stop`);
  const cand = await j("POST", `/api/recordings/${id}/candidates`);
  assert.equal(cand.mappings.length, 3);

  const { thread_id } = await j("POST", "/api/integrations/build", { recording_id: id });
  let p = await waitPending(thread_id, "ask_user");
  await j("POST", `/api/agent/${thread_id}/resume`, { interrupt_id: p.interrupt_id, answer: "Quarantine them for review" });
  p = await waitPending(thread_id, "request_deploy");
  assert.equal(p.version, 1);
  await j("POST", `/api/agent/${thread_id}/resume`, { interrupt_id: p.interrupt_id, decision: "approve" });
  await waitState(thread_id, "done");

  await j("PUT", "/api/settings", { ai_enabled: false });
  const r1 = await waitRun((await j("POST", "/api/runs", { integration: "po_to_stockroom", limit: 100 })).run_id);
  console.log("run 100:", r1.run.counts, r1.run.duration_s, "s");
  assert.equal(r1.records.length, 100);
  await j("POST", "/api/demo/stockroom/schema", { version: 2 });
  const blocked = await waitRun((await j("POST", "/api/runs", { integration: "po_to_stockroom" })).run_id);
  assert.equal(blocked.run.status, "blocked");
  assert.equal(blocked.incident.detail.contract_errors.length, 30);
  await assert.rejects(j("POST", `/api/incidents/${blocked.incident.id}/repair`), (e) => e.status === 409);
  await j("PUT", "/api/settings", { ai_enabled: true });
  const rep = (await j("POST", `/api/incidents/${blocked.incident.id}/repair`)).thread_id;
  p = await waitPending(rep, "ask_user");
  await j("POST", `/api/agent/${rep}/resume`, { interrupt_id: p.interrupt_id, answer: p.options[1] });
  p = await waitPending(rep, "request_deploy");
  assert.equal(p.version, 2);
  assert.ok(p.code_diff.includes("+++ v2/"));
  await j("POST", `/api/agent/${rep}/resume`, { interrupt_id: p.interrupt_id, decision: "approve" });
  await waitState(rep, "done");
  await j("PUT", "/api/settings", { ai_enabled: false });
  await until(async () => (await j("GET", "/api/runs")).every((r) => r.status !== "running"), "replay to finish");
  const r2 = await waitRun((await j("POST", "/api/runs", { integration: "po_to_stockroom" })).run_id);
  console.log("run rest on v2:", r2.run.counts);
  assert.equal(r2.run.status, "completed");
  assert.ok(r2.records.find((r) => r.status === "success").sink_body.warehouse_id);
  const acc = await j("GET", "/api/bench/accuracy?integration=po_to_stockroom");
  console.log("accuracy:", acc.correct, "/", acc.records);
  assert.equal(acc.records, 150);
  assert.equal(acc.correct, 150);
  assert.equal(r2.run.kind, "run");
  assert.equal(r2.run.dry_run, false);
  assert.equal(r2.run.total, 50);
  const replays = (await j("GET", "/api/runs")).filter((r) => r.kind === "replay");
  assert.equal(replays.length, 1);
  assert.equal((await j("GET", `/api/incidents/${blocked.incident.id}`)).status, "resolved");
  await assert.rejects(j("POST", "/api/integrations/po_to_stockroom/rollback", { version: 2 }), (e) => e.status === 409);
  const sse = await (await fetch(`${BASE}/api/runs/${r2.run.id}/stream`)).text();
  assert.match(sse, /id: 50\nevent: progress/);
  assert.match(sse, /id: 51\nevent: finished\ndata: \{"run_id"/);
  const recs = await j("GET", "/api/recordings");
  assert.equal(recs[0].id, "fixture");
  assert.equal(recs[0].fixture, true);
  const diff = await j("GET", "/api/integrations/po_to_stockroom/diff");
  assert.equal(diff.from, 1);
  assert.ok(diff.spec_diff.includes("+    warehouse_id:"));
  const status = await j("GET", "/api/integrations/po_to_stockroom");
  assert.equal(status.active_version, 2);
  assert.equal(status.open_incidents.length, 0);
  // Benchmark tab: serves bench/results.json when `make bench` has written it, else 404.
  const bench = await fetch(`${BASE}/api/bench/results`);
  assert.ok(bench.status === 200 || bench.status === 404, `bench results: ${bench.status}`);
  if (bench.status === 200) assert.equal((await bench.json()).schema_version, 1);
  console.log("smoke OK; runs:", (await j("GET", "/api/runs")).map((r) => `${r.id}:${r.status}${r.dry_run ? "(dry)" : ""}`).join(" "));
} finally {
  proc.kill();
}
