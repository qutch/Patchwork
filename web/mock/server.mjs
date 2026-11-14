// Mock control plane for UI development (docs/contracts.md section 4).
//
//   node mock/server.mjs [--port 5181] [--pace 1]
//
// No dependencies beyond Node's stdlib. Canned data in mock/data.json comes from a real run
// of the compiler, runner and ScriptedAgentService (see mock/collect.py). The server keeps
// in-memory state so the whole 4-minute demo can be clicked through: record -> candidates ->
// BUILD thread (ask_user, request_deploy) -> runs with AI off -> Break 1 -> REPAIR -> run again.
//
// PACE scales every delay (0 = instant, handy for tests).

import http from "node:http";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));
const DATA = JSON.parse(readFileSync(join(HERE, "data.json"), "utf8"));
const arg = (name, dflt) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 ? process.argv[i + 1] : dflt;
};
const PORT = Number(arg("port", process.env.MOCK_PORT || 5181));
const PACE = Number(arg("pace", process.env.MOCK_PACE ?? 1));
const NAME = "po_to_stockroom";
const RECORD_MS = 45; // per record in a run, so 100 records take ~4.5 s on stage

const nowIso = () => new Date().toISOString().replace(/\.\d+Z$/, "+00:00");
const clone = (x) => JSON.parse(JSON.stringify(x));
const later = (ms, fn) => setTimeout(fn, Math.round(ms * PACE));

// ------------------------------------------------------------------------------ state
let S;
function freshState(keepSettings) {
  const settings = keepSettings ? S.settings : { ai_enabled: true, agent: "scripted", model: "none (scripted agent)" };
  for (const t of S?.timers ?? []) clearTimeout(t);
  S = {
    settings,
    timers: [],
    recordings: new Map(),
    threads: new Map(),
    versions: [], // {version, created_at, deployed_at, tests_passed, tests_summary, detail, contract, warehouse}
    active: null,
    runs: [], // {row, records: [], progress: [], listeners:Set, done}
    incidents: [],
    inbox: DATA.messages.map((m) => ({ ...m })),
    schema: { live: 1, staging: 1 },
    processed: { live: new Set(), staging: new Set() },
    cursor: { live: 0, staging: 0 }, // index into inbox
    delivered: 0,
  };
}
freshState(false);
const timer = (ms, fn) => {
  const t = later(ms, fn);
  S.timers.push(t);
  return t;
};

// ------------------------------------------------------------------------------ helpers
const send = (res, status, body) => {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
};
const fail = (res, status, detail) => send(res, status, { detail });
const readBody = (req) =>
  new Promise((resolve) => {
    let b = "";
    req.on("data", (c) => (b += c));
    req.on("end", () => {
      try {
        resolve(b ? JSON.parse(b) : {});
      } catch {
        resolve({});
      }
    });
  });

/** SSE: replay everything after `after`, then follow. `items()` returns [{id, event, data}]. */
function sse(req, res, url, src) {
  const header = Number(req.headers["last-event-id"] || 0);
  const after = Math.max(header, Number(url.searchParams.get("after") || 0));
  res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "keep-alive" });
  res.write("retry: 1000\n\n");
  const write = (m) => {
    res.write(`id: ${m.id}\nevent: ${m.event}\ndata: ${JSON.stringify(m.data)}\n\n`);
    if (src.terminal?.(m)) end();
  };
  let ended = false;
  const end = () => {
    if (ended) return;
    ended = true;
    src.listeners.delete(write);
    clearInterval(ping);
    res.end();
  };
  const ping = setInterval(() => res.write(": ping\n\n"), 15000);
  src.listeners.add(write);
  req.on("close", end);
  for (const m of src.items()) if (m.id > after && !ended) write(m);
  if (!ended && src.isOver?.()) end();
}
const broadcast = (listeners, m) => {
  for (const l of [...listeners]) l(m);
};

// ------------------------------------------------------------------------------ unified diff
function unifiedDiff(a, b, fromName, toName, ctx = 3) {
  const A = a ? a.split("\n") : [];
  const B = b ? b.split("\n") : [];
  if (a === b) return "";
  const n = A.length, m = B.length;
  const L = Array.from({ length: n + 1 }, () => new Int32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i][j] = A[i] === B[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const ops = []; // [type, lineA, lineB, text]
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (A[i] === B[j]) ops.push([" ", i++, j++]);
    else if (L[i + 1][j] >= L[i][j + 1]) ops.push(["-", i++, j]);
    else ops.push(["+", i, j++]);
  }
  while (i < n) ops.push(["-", i++, j]);
  while (j < m) ops.push(["+", i, j++]);
  const out = [`--- ${fromName}`, `+++ ${toName}`];
  let k = 0;
  while (k < ops.length) {
    while (k < ops.length && ops[k][0] === " ") k++;
    if (k >= ops.length) break;
    let start = Math.max(0, k - ctx), end = k;
    for (;;) {
      while (end < ops.length && ops[end][0] !== " ") end++;
      let next = end;
      while (next < ops.length && ops[next][0] === " ") next++;
      if (next < ops.length && next - end <= ctx * 2) end = next;
      else {
        end = Math.min(ops.length, end + ctx);
        break;
      }
    }
    const hunk = ops.slice(start, end);
    const aStart = hunk[0][1] + 1, bStart = hunk[0][2] + 1;
    const aLen = hunk.filter((o) => o[0] !== "+").length, bLen = hunk.filter((o) => o[0] !== "-").length;
    out.push(`@@ -${aLen ? aStart : aStart - 1},${aLen} +${bLen ? bStart : bStart - 1},${bLen} @@`);
    for (const o of hunk) out.push(o[0] + (o[0] === "+" ? B[o[2]] : A[o[1]]));
    k = end;
  }
  return out.join("\n") + "\n";
}

/** Minimal JSON Patch generator (add/remove/replace), good enough for spec diffs. */
function jsonPatch(a, b, path = "") {
  if (JSON.stringify(a) === JSON.stringify(b)) return [];
  const isObj = (x) => x && typeof x === "object" && !Array.isArray(x);
  if (isObj(a) && isObj(b)) {
    const ops = [];
    for (const k of Object.keys(a)) if (!(k in b)) ops.push({ op: "remove", path: `${path}/${k}` });
    for (const k of Object.keys(b)) {
      if (!(k in a)) ops.push({ op: "add", path: `${path}/${k}`, value: b[k] });
      else ops.push(...jsonPatch(a[k], b[k], `${path}/${k}`));
    }
    return ops;
  }
  return [{ op: "replace", path: path || "/", value: b }];
}

// ------------------------------------------------------------------------------ versions
function addVersion(kind, warehouse) {
  const version = (S.versions.at(-1)?.version ?? 0) + 1;
  const src = kind === "build" ? DATA.v1 : DATA.repair[warehouse === "supplier" ? "by_supplier" : "always_bos"].v2;
  const detail = clone(src);
  detail.version = version;
  detail.manifest = { ...detail.manifest, version, compiled_at: nowIso() };
  const v = {
    version,
    created_at: nowIso(),
    deployed_at: null,
    tests_passed: detail.tests_passed,
    tests_summary: detail.tests_summary,
    detail,
    contract: kind === "build" ? 1 : 2,
    warehouse: warehouse ?? null,
  };
  S.versions.push(v);
  return v;
}
const getVersion = (n) => S.versions.find((v) => v.version === Number(n));
function deploy(n) {
  const v = getVersion(n);
  if (!v) return [404, `${NAME} v${n} does not exist`];
  if (!v.tests_passed) return [409, `${NAME} v${n}: tests have not passed on this version`];
  const previous = S.active;
  S.active = v.version;
  v.deployed_at = nowIso();
  return [200, { name: NAME, active_version: v.version, previous_version: previous }];
}

// ------------------------------------------------------------------------------ runs
const PO_RE = /(PO-\d+)/;
function bodyFor(msg, v) {
  const t = msg.truth;
  const b1 = t.body ?? { product_id: t.sku, quantity: t.qty, reference: t.po };
  if (v.contract === 1) return { ...b1 };
  const wh = v.warehouse === "supplier" ? DATA.supplier_warehouse[msg.sender_domain] ?? "BOS" : "BOS";
  return { reference: b1.reference, sku: b1.product_id, quantity_delta: b1.quantity, warehouse_id: wh };
}

function processMsg(msg, v, scope) {
  const out = {
    record_id: msg.id, status: "quarantined", reason: null, detail: null, parser: msg.parser ?? null,
    route_value: msg.sender_domain, dedupe_key: null, sink_body: null, raw: stripMsg(msg),
  };
  if (msg.sender_domain === "umbrella.com") {
    out.reason = "PARSE_NO_MATCH";
    out.detail = `no parser for sender_domain='umbrella.com'`;
    return out;
  }
  const t = msg.truth;
  if (t.outcome === "quarantined") {
    out.reason = t.reason;
    out.detail = msg.detail;
    return out;
  }
  const key = msg.key ?? (msg.body.match(PO_RE) || [])[1];
  out.dedupe_key = key;
  if (S.processed[scope].has(key)) {
    out.status = "duplicate";
    return out;
  }
  out.sink_body = bodyFor(msg, v);
  out.status = "success";
  S.processed[scope].add(key);
  return out;
}
const stripMsg = (m) => ({ id: m.id, sender: m.sender, sender_domain: m.sender_domain, subject: m.subject, body: m.body, received_at: m.received_at });

function contractErrors(v, schema) {
  if (v.contract === 1 && schema === 2) return clone(DATA.incident.detail.contract_errors);
  const names = ["acme_msg_0007", "acme_msg_0012", "globex_msg_0001", "globex_msg_0005", "initech_msg_0002", "initech_msg_0003"];
  const errs = [];
  for (const n of names) {
    for (const f of ["product_id", "quantity"]) errs.push(`[${n}] body.${f}: required by destination but missing from body`);
    for (const f of ["quantity_delta", "sku", "warehouse_id"]) errs.push(`[${n}] body.${f}: not accepted by destination (no such property)`);
  }
  return errs.sort();
}

function openIncident(fields) {
  const inc = { id: S.incidents.length + 1, integration: NAME, status: "open", created_at: nowIso(), detail: null, samples: null, old_openapi: null, new_openapi: null, ...fields };
  S.incidents.push(inc);
  return inc;
}

/** Start a run. `pick` optionally selects records (replay); otherwise new inbox records after the cursor. */
function startRun({ dry_run = false, limit = null, replay = null }) {
  if (S.active == null) return [409, `${NAME}: no active version; deploy one first`];
  const v = getVersion(S.active);
  const scope = dry_run ? "staging" : "live";
  const row = {
    id: S.runs.length + 1, integration: NAME, version: v.version, dry_run: dry_run ? 1 : 0, status: "running",
    started_at: nowIso(), finished_at: null, duration_s: null, counts: {}, llm_calls: 0, incident_id: null, message: null,
  };
  const run = { row, records: [], events: [], listeners: new Set(), done: false, t0: Date.now() };
  S.runs.push(run);
  const emit = (event, data) => {
    const m = { id: run.events.length + 1, event, data };
    run.events.push(m);
    broadcast(run.listeners, m);
  };
  const finish = (status, message = null) => {
    row.status = status;
    row.message = message;
    row.finished_at = nowIso();
    row.duration_s = Math.round((Date.now() - run.t0) / 10) / 100;
    run.done = true;
    const summary = { run_id: row.id, integration: NAME, version: row.version, dry_run: !!dry_run, status, counts: row.counts,
      duration_s: row.duration_s, llm_calls: 0, incident_id: row.incident_id, message, log: `workspace/runs/${row.id}.jsonl` };
    emit("finished", { summary });
    return summary;
  };

  let todo;
  if (replay) todo = replay;
  else {
    todo = S.inbox.slice(S.cursor[scope]);
    if (limit != null && limit !== "") todo = todo.slice(0, Number(limit));
  }
  // preflight
  timer(350, () => {
    const schema = S.schema[scope];
    if (schema !== v.contract) {
      const errs = contractErrors(v, schema);
      const inc = openIncident({
        version: v.version, run_id: row.id, type: "CONTRACT_CHANGED", reason_codes: ["SINK_CONTRACT"],
        summary: `Destination contract changed; ${errs.length} contract errors. Blocked before any write.`,
        detail: { contract_errors: errs, sink_call: { method: "POST", path: "/api/incoming" } },
        old_openapi: v.contract === 1 ? DATA.incident.old_openapi : DATA.incident.new_openapi,
        new_openapi: schema === 2 ? DATA.incident.new_openapi : DATA.incident.old_openapi,
      });
      row.incident_id = inc.id;
      finish("blocked", "preflight: destination contract changed");
      return;
    }
    emit("progress", { counts: { ...row.counts }, processed: 0, total: todo.length });
    let i = 0;
    const step = () => {
      if (i >= todo.length) {
        const byRoute = {};
        for (const r of run.records) if (r.reason === "PARSE_NO_MATCH") byRoute[r.route_value] = (byRoute[r.route_value] ?? 0) + 1;
        for (const [route, n] of Object.entries(byRoute)) {
          if (n >= 3 && row.incident_id == null) {
            const samples = run.records.filter((r) => r.reason === "PARSE_NO_MATCH" && r.route_value === route).slice(0, 5)
              .map((r) => ({ record_id: r.record_id, detail: r.detail, record: r.raw }));
            const inc = openIncident({ version: v.version, run_id: row.id, type: "NEW_SOURCE_FORMAT", reason_codes: ["PARSE_NO_MATCH"],
              summary: `${n} records from sender_domain=${route} matched no parser.`, samples });
            row.incident_id = inc.id;
          }
        }
        finish("completed");
        return;
      }
      const msg = todo[i++];
      const out = { run_id: row.id, ...processMsg(msg, v, scope) };
      run.records.push(out);
      row.counts[out.status] = (row.counts[out.status] ?? 0) + 1;
      if (out.reason) row.counts[`quarantined:${out.reason}`] = (row.counts[`quarantined:${out.reason}`] ?? 0) + 1;
      if (!replay) S.cursor[scope] = Math.max(S.cursor[scope], S.inbox.indexOf(msg) + 1);
      emit("progress", { counts: { ...row.counts }, processed: i, total: todo.length });
      timer(RECORD_MS, step);
    };
    step();
  });
  return [200, { run_id: row.id }];
}

function latestLiveOutcomes() {
  const latest = new Map();
  for (const run of S.runs) if (!run.row.dry_run) for (const r of run.records) latest.set(r.record_id, r);
  return latest;
}

function replayQuarantined() {
  const ids = [...latestLiveOutcomes().values()].filter((r) => r.status === "quarantined").map((r) => r.record_id);
  const msgs = ids.map((id) => S.inbox.find((m) => m.id === id)).filter(Boolean);
  return startRun({ replay: msgs });
}

function accuracy() {
  const recs = [...latestLiveOutcomes().values()];
  let right = 0;
  const mismatches = [];
  for (const r of recs) {
    const msg = S.inbox.find((m) => m.id === r.record_id);
    const gt = msg.truth;
    let ok = r.status === gt.outcome && (r.reason ?? null) === (gt.reason ?? null);
    if (ok && gt.outcome === "success") {
      const exp = r.sink_body && "sku" in r.sink_body ? gt.body_v2 : gt.body;
      ok = JSON.stringify(sortKeys(r.sink_body)) === JSON.stringify(sortKeys(exp));
    }
    if (ok) right++;
    else if (mismatches.length < 20) mismatches.push({ record_id: r.record_id, got: [r.status, r.reason, r.sink_body], expected: [gt.outcome, gt.reason, gt.body ?? null] });
  }
  return { records: recs.length, correct: right, accuracy: recs.length ? Math.round((right / recs.length) * 10000) / 10000 : null, mismatches };
}
const sortKeys = (o) => (o ? Object.fromEntries(Object.entries(o).sort(([a], [b]) => a.localeCompare(b))) : o);

// ------------------------------------------------------------------------------ inbox
const CATALOG = [...Array.from({ length: 30 }, (_, i) => `WID-${100 + i}`), ...Array.from({ length: 20 }, (_, i) => `GAD-${200 + i}`)];
let rngState = 7;
const rand = (n) => {
  rngState = (rngState * 1103515245 + 12345) % 2147483648;
  return rngState % n;
};
function render(domain, po, sku, qty) {
  if (domain === "acme.com") return [`Purchase Order ${po}`, `Hi team,\n\nPlease find our order below.\n\nPO Number: ${po}\nSKU: ${sku}\nQty: ${qty}\n\nThanks,\nAcme Supply`, "orders@acme.com", "acme_v1"];
  if (domain === "globex.com") return [`Globex order ${po}`, `Hello Stockroom,\n\nThis is Globex purchase order ${po}. Please receive ${qty} units of item ${sku} at your earliest convenience.\n\nRegards,\nGlobex Procurement`, "procurement@globex.com", "globex_v1"];
  return [`[ERP] ${po}`, `ORDER REF=${po} | PART=${sku} | UNITS=${qty}\nSent automatically from Initech ERP`, "erp-noreply@initech.com", "initech_v1"];
}
function nextPo(domain) {
  const nums = S.inbox.filter((m) => m.sender_domain === domain).map((m) => Number((m.body.match(/PO-(\d+)/) || [])[1] || 0));
  const base = { "acme.com": 1000, "globex.com": 2000, "initech.com": 3000, "umbrella.com": 4000 }[domain];
  return `PO-${Math.max(base, ...nums) + 1}`;
}
function appendMsg(m) {
  const lastT = new Date(S.inbox.at(-1).received_at).getTime();
  m.id = `msg-${String(S.inbox.length + 1).padStart(4, "0")}`;
  m.received_at = new Date(lastT + (5 + rand(30)) * 60000).toISOString().replace(/\.\d+Z$/, "Z");
  S.inbox.push(m);
}
function deliver(count) {
  const domains = ["acme.com", "globex.com", "initech.com"];
  for (let k = 0; k < count; k++) {
    const domain = domains[(S.delivered + k) % 3];
    const po = nextPo(domain), sku = CATALOG[rand(CATALOG.length)], qty = 1 + rand(120);
    const [subject, body, sender, parser] = render(domain, po, sku, qty);
    appendMsg({ sender, sender_domain: domain, subject, body, parser, key: po, detail: null, status: "success", reason: null,
      truth: { outcome: "success", reason: null, body: { product_id: sku, quantity: qty, reference: po },
        body_v2: { sku, quantity_delta: qty, reference: po, warehouse_id: DATA.supplier_warehouse[domain] } } });
  }
  S.delivered += count;
  return { delivered: count, total: S.inbox.length };
}
function newSupplier(count) {
  for (let k = 0; k < count; k++) {
    const po = nextPo("umbrella.com"), sku = CATALOG[rand(CATALOG.length)], cases = 1 + rand(4);
    appendMsg({ sender: "purchasing@umbrella.com", sender_domain: "umbrella.com", subject: `Umbrella PO ${po}`,
      body: `Purchase ref ${po}\nItem code: ${sku} / ${cases} cases (12/case)\n-- Umbrella Purchasing`, parser: null, key: po,
      truth: { outcome: "success", reason: null, body: { product_id: sku, quantity: cases * 12, reference: po },
        body_v2: { sku, quantity_delta: cases * 12, reference: po, warehouse_id: "BOS" } } });
  }
  return { delivered: count, total: S.inbox.length };
}

// ------------------------------------------------------------------------------ recordings
function newRecId() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `rec-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}-${Math.random().toString(16).slice(2, 6)}`;
}
function startRecording({ intent, allowlist, start_url }) {
  const id = newRecId();
  const rec = { id, intent: intent || DATA.recording.intent, allowlist: allowlist?.length ? allowlist : DATA.recording.allowlist,
    start_url: start_url ?? null, status: "recording", started_at: Date.now(), stopped_at: null, events: [], candidates: null,
    listeners: new Set(), stream: [] };
  S.recordings.set(id, rec);
  const fx = DATA.recording.events;
  const t0 = fx[0].t;
  fx.forEach((e, i) => {
    timer(600 + (e.t - t0) * 0.45, () => {
      if (rec.status !== "recording") return;
      const ev = { ...clone(e), t: Date.now() };
      rec.events.push(ev);
      const m = { id: rec.stream.length + 1, event: "capture", data: ev };
      rec.stream.push(m);
      broadcast(rec.listeners, m);
      if (i === fx.length - 1) rec.complete = true;
    });
  });
  return { id };
}
const recSummary = (r) => ({ id: r.id, intent: r.intent, allowlist: r.allowlist, status: r.status, started_at: r.started_at, stopped_at: r.stopped_at, n_events: r.events.length });
function stopRecording(r) {
  if (r.status === "recording") {
    r.status = "stopped";
    r.stopped_at = Date.now();
    const m = { id: r.stream.length + 1, event: "stopped", data: recSummary(r) };
    r.stream.push(m);
    broadcast(r.listeners, m);
  }
  return recSummary(r);
}
function fixtureRecording() {
  const r = DATA.recording;
  return { id: "fixture", intent: r.intent, allowlist: r.allowlist, status: "stopped", started_at: r.started_at, stopped_at: r.stopped_at,
    n_events: r.events.length, events: r.events, candidates: { ...DATA.candidates, recording_id: "fixture" } };
}

// ------------------------------------------------------------------------------ agent threads
const PACING = { tool_call: 650, tool_result: 300, message: 700, subagent_start: 500, subagent_end: 400, todos: 300, status: 120, artifact: 300, deployed: 400, done: 200 };

function startThread(mode, subject) {
  const id = `${mode}-${Math.random().toString(16).slice(2, 12)}`;
  const script = mode === "build" ? clone(DATA.build_events) : clone(DATA.repair.by_supplier.events);
  const t = { id, mode, subject: String(subject), state: "running", pending: null, events: [], created_at: nowIso(), listeners: new Set(),
    script, pos: 0, version: null, warehouse: null, deployVersion: null };
  S.threads.set(id, t);
  timer(300, () => advance(t));
  return id;
}

function emitAgent(t, ev) {
  const e = { ...ev, seq: t.events.length + 1, t: new Date().toISOString(), thread_id: t.id };
  t.events.push(e);
  if (e.type === "status") t.state = e.state;
  broadcast(t.listeners, { id: e.seq, event: e.type, data: e });
  return e;
}
const strip = ({ seq, t, thread_id, ...rest }) => rest; // eslint-disable-line no-unused-vars

function advance(t) {
  if (t.pos >= t.script.length || t.state === "waiting") return;
  const raw = strip(t.script[t.pos++]);
  // recorded "resumed"/"deployed"/"done" are produced from the real resume instead
  if (raw.type === "resumed" || raw.type === "deployed" || raw.type === "done") return advance(t);
  if (raw.type === "status" && (raw.state === "done" || raw.state === "failed")) return;
  if (raw.type === "status" && raw.state === "running" && t.events.length > 0) return advance(t);
  if (raw.type === "status" && raw.state === "waiting") return advance(t); // emitted with the interrupt
  sideEffects(t, raw);
  const ev = emitAgent(t, raw);
  if (raw.type === "ask_user" || raw.type === "request_deploy") {
    t.pending = ev;
    emitAgent(t, { type: "status", state: "waiting", mode: t.mode });
    return;
  }
  timer(PACING[raw.type] ?? 300, () => advance(t));
}

/** Rewrite recorded version numbers to the mock's next version, and do what the real tools did. */
function sideEffects(t, ev) {
  const recorded = t.mode === "build" ? 1 : 2;
  const fix = (s) => (typeof s === "string" ? s.replaceAll(`v${recorded}`, `v${t.version ?? recorded}`) : s);
  if (ev.type === "tool_result" && ev.tool === "compile_spec") {
    const v = addVersion(t.mode, t.warehouse);
    t.version = v.version;
  }
  if (t.version != null) {
    if (ev.summary) ev.summary = fix(ev.summary);
    if (ev.args?.version != null) ev.args.version = t.version;
    if (ev.type === "artifact" && ev.version != null) {
      ev.version = t.version;
      ev.path = ev.path.replace(`/v${recorded}`, `/v${t.version}`);
    }
  }
  if (ev.type === "tool_result" && ev.tool === "dry_run") {
    const prev = S.active;
    // run the dry run on the candidate version, against staging
    S.active = t.version;
    startRun({ dry_run: true, limit: 10 });
    S.active = prev;
  }
  if (ev.type === "tool_result" && ev.tool === "get_incident") {
    const inc = S.incidents.find((i) => String(i.id) === t.subject);
    if (inc) ev.summary = inc.summary;
  }
  if (ev.type === "subagent_start" && ev.name === "repairer") {
    const inc = S.incidents.find((i) => String(i.id) === t.subject);
    if (inc) ev.task = `Incident #${inc.id}: ${inc.type}`;
  }
  if (ev.type === "request_deploy") {
    ev.version = t.version;
    ev.tests = { passed: true, summary: getVersion(t.version)?.tests_summary ?? ev.tests.summary };
    const prev = S.active ? getVersion(S.active) : null;
    const cur = getVersion(t.version);
    if (prev && cur) {
      ev.code_diff = ["integration.py", "parsers.py", "adapters.py"]
        .map((f) => unifiedDiff(prev.detail.files[f], cur.detail.files[f], `v${prev.version}/${f}`, `v${cur.version}/${f}`)).join("");
      if (t.mode === "repair") {
        ev.spec_diff = unifiedDiff(prev.detail.spec_yaml, cur.detail.spec_yaml, "spec.yaml (active)", `spec.yaml (v${cur.version})`);
      }
    } else if (cur) {
      ev.code_diff = "";
      ev.spec_diff = cur.detail.spec_yaml;
    }
  }
}

function resume(t, body) {
  if (!t.pending || t.pending.interrupt_id !== body.interrupt_id) return [404, `no pending interrupt ${body.interrupt_id} on ${t.id}`];
  const pending = t.pending;
  const payload = pending.type === "ask_user" ? { answer: String(body.answer ?? "") } : { decision: body.decision, comment: body.comment ?? "" };
  if (pending.type === "request_deploy" && !["approve", "reject"].includes(payload.decision)) return [422, "decision must be approve or reject"];
  t.pending = null;
  emitAgent(t, { type: "resumed", interrupt_id: pending.interrupt_id, payload });
  emitAgent(t, { type: "status", state: "running", mode: t.mode });

  if (pending.type === "ask_user") {
    if (t.mode === "repair") {
      const bySupplier = payload.answer.toLowerCase().includes("supplier");
      t.warehouse = bySupplier ? "supplier" : "bos";
      // switch to the storyline recorded with this answer, from the same position
      t.script = clone(DATA.repair[bySupplier ? "by_supplier" : "always_bos"].events);
    } else {
      // the recorded follow-up message quotes the answer
      const next = t.script.slice(t.pos).find((e) => e.type === "message" && e.text.startsWith("Recorded assumption"));
      if (next) next.text = `Recorded assumption a2: ${payload.answer}.`;
    }
    timer(400, () => advance(t));
    return [200, { ok: true }];
  }

  // request_deploy
  const finishThread = (result) => {
    emitAgent(t, { type: "done", result });
    emitAgent(t, { type: "status", state: "done", mode: t.mode });
  };
  if (payload.decision !== "approve") {
    timer(300, () => {
      emitAgent(t, { type: "message", agent: "supervisor", text: `Deploy of v${pending.version} rejected: ${payload.comment}` });
      finishThread({ deployed: false, version: pending.version });
    });
    return [200, { ok: true }];
  }
  timer(400, () => {
    const [code, res] = deploy(pending.version);
    if (code !== 200) {
      emitAgent(t, { type: "error", message: `DeployRefused: ${res}` });
      emitAgent(t, { type: "status", state: "failed", mode: t.mode });
      return;
    }
    emitAgent(t, { type: "deployed", name: NAME, version: pending.version, previous_version: res.previous_version });
    if (t.mode === "repair") {
      const inc = S.incidents.find((i) => String(i.id) === t.subject);
      if (inc) inc.status = "resolved";
      timer(300, () => {
        emitAgent(t, { type: "message", agent: "supervisor", text: "Replaying quarantined records on the new version." });
        replayQuarantined();
        timer(300, () => finishThread({ deployed: true, version: pending.version }));
      });
    } else timer(200, () => finishThread({ deployed: true, version: pending.version }));
  });
  return [200, { ok: true }];
}

function threadStatus(t) {
  return { thread_id: t.id, mode: t.mode, subject: t.subject, state: t.state, pending: t.pending, last_seq: t.events.length, created_at: t.created_at };
}

function startRepair(incidentId) {
  const inc = S.incidents.find((i) => i.id === Number(incidentId));
  if (!inc) return [404, `incident ${incidentId} not found`];
  if (inc.type !== "CONTRACT_CHANGED") {
    // the scripted agent only knows Break 1; mirror its failure honestly
    const id = `repair-${Math.random().toString(16).slice(2, 12)}`;
    const t = { id, mode: "repair", subject: String(inc.id), state: "running", pending: null, events: [], created_at: nowIso(), listeners: new Set(), script: [], pos: 0 };
    S.threads.set(id, t);
    timer(200, () => {
      emitAgent(t, { type: "status", state: "running", mode: "repair" });
      emitAgent(t, { type: "subagent_start", name: "repairer", task: `Incident #${inc.id}: ${inc.type}` });
      emitAgent(t, { type: "tool_call", agent: "repairer", tool: "get_incident", args: { incident_id: inc.id } });
      emitAgent(t, { type: "tool_result", agent: "repairer", tool: "get_incident", ok: true, summary: inc.summary });
      emitAgent(t, { type: "error", message: `RuntimeError: scripted repair only handles CONTRACT_CHANGED, got ${inc.type}` });
      emitAgent(t, { type: "status", state: "failed", mode: "repair" });
    });
    return [200, { thread_id: id }];
  }
  return [200, { thread_id: startThread("repair", inc.id) }];
}

// ------------------------------------------------------------------------------ routes
const incidentSummary = ({ id, integration, version, run_id, type, reason_codes, summary, status, created_at }) =>
  ({ id, integration, version, run_id, type, reason_codes, summary, status, created_at });
const versionRow = (v) => ({ version: v.version, tests_passed: v.tests_passed, tests_summary: v.tests_summary, created_at: v.created_at, deployed_at: v.deployed_at });
const openIncidents = () => S.incidents.filter((i) => i.status === "open").map(incidentSummary).reverse();

async function route(req, res) {
  const url = new URL(req.url, "http://x");
  const p = url.pathname;
  const m = (re) => p.match(re);
  const M = req.method;
  let mm;
  const body = M === "POST" || M === "PUT" ? await readBody(req) : {};

  if (M === "GET" && p === "/api/health") return send(res, 200, { ok: true, mock: true });
  if (p === "/api/settings") {
    if (M === "PUT") S.settings.ai_enabled = !!body.ai_enabled;
    return send(res, 200, S.settings);
  }

  // recordings
  if (M === "POST" && p === "/api/recordings") return send(res, 200, startRecording(body));
  if (M === "GET" && p === "/api/recordings") return send(res, 200, [...S.recordings.values()].map(recSummary).reverse());
  if ((mm = m(/^\/api\/recordings\/([^/]+)(\/[a-z]+)?$/))) {
    const id = decodeURIComponent(mm[1]);
    const sub = mm[2];
    if (id === "fixture" && M === "GET" && !sub) return send(res, 200, fixtureRecording());
    if (id === "fixture" && sub === "/candidates") return send(res, 200, { ...DATA.candidates, recording_id: "fixture" });
    const r = S.recordings.get(id);
    if (!r) return fail(res, 404, `recording ${id} not found`);
    if (M === "GET" && !sub) return send(res, 200, { ...recSummary(r), events: r.events, candidates: r.candidates });
    if (M === "POST" && sub === "/stop") return send(res, 200, stopRecording(r));
    if (M === "POST" && sub === "/candidates") {
      if (!r.events.some((e) => e.type === "net" && e.method === "POST"))
        return fail(res, 422, "no mutating request to an allowlisted host was captured; transfer one record before stopping");
      r.candidates = { ...clone(DATA.candidates), recording_id: r.id, intent: r.intent };
      return send(res, 200, r.candidates);
    }
    if (M === "GET" && sub === "/stream")
      return sse(req, res, url, { listeners: r.listeners, items: () => r.stream, terminal: (x) => x.event === "stopped", isOver: () => r.status === "stopped" });
  }

  // agents
  if (M === "POST" && p === "/api/integrations/build") {
    if (!S.settings.ai_enabled) return fail(res, 409, "AI is off. Turn AI on to build an integration.");
    const rid = body.recording_id;
    if (rid !== "fixture" && !S.recordings.has(rid)) return fail(res, 404, `recording ${rid} not found`);
    return send(res, 200, { thread_id: startThread("build", rid) });
  }
  if (M === "POST" && (mm = m(/^\/api\/incidents\/(\d+)\/repair$/))) {
    if (!S.settings.ai_enabled) return fail(res, 409, "AI is off. Turn AI on to repair.");
    const [code, out] = startRepair(mm[1]);
    return code === 200 ? send(res, 200, out) : fail(res, code, out);
  }
  if (M === "GET" && p === "/api/agent/threads") return send(res, 200, [...S.threads.values()].map(threadStatus));
  if ((mm = m(/^\/api\/agent\/([^/]+)(\/[a-z]+)?$/))) {
    const t = S.threads.get(decodeURIComponent(mm[1]));
    if (!t) return fail(res, 404, `thread ${mm[1]} not found`);
    if (M === "GET" && !mm[2]) return send(res, 200, threadStatus(t));
    if (M === "GET" && mm[2] === "/stream")
      return sse(req, res, url, { listeners: t.listeners, items: () => t.events.map((e) => ({ id: e.seq, event: e.type, data: e })) });
    if (M === "POST" && mm[2] === "/resume") {
      const [code, out] = resume(t, body);
      return code === 200 ? send(res, 200, out) : fail(res, code, out);
    }
  }

  // integrations
  if (M === "GET" && p === "/api/integrations")
    return send(res, 200, S.versions.length ? [{ name: NAME, active_version: S.active, latest_version: S.versions.at(-1).version, open_incidents: openIncidents().length }] : []);
  if ((mm = m(/^\/api\/integrations\/([^/]+)(\/.*)?$/))) {
    const name = decodeURIComponent(mm[1]);
    const sub = mm[2] ?? "";
    if (name !== NAME || !S.versions.length) return fail(res, 404, `integration ${name} not found`);
    if (M === "GET" && !sub)
      return send(res, 200, { name, active_version: S.active, versions: S.versions.map(versionRow),
        recent_runs: S.runs.slice(-5).reverse().map((r) => pickRun(r.row)), open_incidents: openIncidents() });
    let vm;
    if (M === "GET" && (vm = sub.match(/^\/versions\/(\d+)$/))) {
      const v = getVersion(vm[1]);
      if (!v) return fail(res, 404, `${name} v${vm[1]} does not exist`);
      return send(res, 200, { ...v.detail, tests_passed: v.tests_passed, tests_summary: v.tests_summary });
    }
    if (M === "GET" && sub === "/diff") {
      const a = getVersion(url.searchParams.get("from")), b = getVersion(url.searchParams.get("to"));
      if (!a || !b) return fail(res, 404, "unknown version");
      const files = [...new Set([...Object.keys(a.detail.files), ...Object.keys(b.detail.files)])].filter((f) => !f.startsWith("tests/") && f !== "spec.yaml").sort();
      const code_diff = {};
      for (const f of files) {
        const d = unifiedDiff(a.detail.files[f] ?? "", b.detail.files[f] ?? "", `v${a.version}/${f}`, `v${b.version}/${f}`);
        if (d) code_diff[f] = d;
      }
      const yamlA = a.detail.spec_yaml, yamlB = b.detail.spec_yaml;
      let spec_patch = [];
      const canonical = a.contract === 1 && b.contract === 2 && b.warehouse ? DATA.repair[b.warehouse === "supplier" ? "by_supplier" : "always_bos"].diff.spec_patch : null;
      if (canonical) spec_patch = canonical;
      else if (yamlA !== yamlB) spec_patch = jsonPatch({ spec: yamlA.split("\n") }, { spec: yamlB.split("\n") });
      return send(res, 200, { spec_patch, spec_diff: unifiedDiff(yamlA, yamlB, `v${a.version}/spec.yaml`, `v${b.version}/spec.yaml`), code_diff });
    }
    if (M === "POST" && (sub === "/deploy" || sub === "/rollback")) {
      const [code, out] = deploy(body.version);
      return code === 200 ? send(res, 200, out) : fail(res, code, out);
    }
    if (M === "POST" && sub === "/replay") {
      const [code, out] = replayQuarantined();
      return code === 200 ? send(res, 200, out) : fail(res, code, out);
    }
  }

  // runs
  if (M === "POST" && p === "/api/runs") {
    const [code, out] = startRun({ dry_run: !!body.dry_run, limit: body.limit ?? null });
    return code === 200 ? send(res, 200, out) : fail(res, code, out);
  }
  if (M === "GET" && p === "/api/runs") {
    const limit = Number(url.searchParams.get("limit") || 20);
    return send(res, 200, S.runs.slice().reverse().slice(0, limit).map((r) => pickRun(r.row)));
  }
  if ((mm = m(/^\/api\/runs\/(\d+)(\/stream)?$/))) {
    const run = S.runs.find((r) => r.row.id === Number(mm[1]));
    if (!run) return fail(res, 404, `run ${mm[1]} not found`);
    if (mm[2]) return sse(req, res, url, { listeners: run.listeners, items: () => run.events, terminal: (x) => x.event === "finished", isOver: () => run.done });
    const inc = run.row.incident_id ? S.incidents.find((i) => i.id === run.row.incident_id) : null;
    return send(res, 200, { run: pickRun(run.row), records: run.records.slice().sort((a, b) => a.record_id.localeCompare(b.record_id)), incident: inc ?? null });
  }
  if (M === "GET" && p === "/api/incidents") {
    const st = url.searchParams.get("status");
    return send(res, 200, S.incidents.filter((i) => !st || i.status === st).map(incidentSummary).reverse());
  }
  if (M === "GET" && (mm = m(/^\/api\/incidents\/(\d+)$/))) {
    const inc = S.incidents.find((i) => i.id === Number(mm[1]));
    return inc ? send(res, 200, inc) : fail(res, 404, `incident ${mm[1]} not found`);
  }

  // demo controls
  if (M === "POST" && p === "/api/demo/reset") {
    freshState(true);
    return send(res, 200, { ok: true });
  }
  if (M === "POST" && p === "/api/demo/stockroom/schema") {
    const v = Number(body.version);
    if (![1, 2].includes(v)) return fail(res, 422, "version must be 1 or 2");
    S.schema.live = S.schema.staging = v;
    return send(res, 200, { live: v, staging: v });
  }
  if (M === "POST" && p === "/api/demo/inbox/deliver") return send(res, 200, deliver(Math.max(1, Number(body.count) || 10)));
  if (M === "POST" && p === "/api/demo/inbox/new_supplier") return send(res, 200, newSupplier(Math.max(1, Number(body.count) || 5)));
  if (M === "GET" && p === "/api/bench/accuracy") return send(res, 200, accuracy());

  return fail(res, 404, `no route ${M} ${p}`);
}
const pickRun = (row) => ({ ...row, counts: { ...row.counts } });

const server = http.createServer((req, res) => {
  route(req, res).catch((e) => {
    console.error(e);
    if (!res.headersSent) fail(res, 500, String(e?.message ?? e));
  });
});
server.listen(PORT, "127.0.0.1", () => console.log(`mock control plane on http://127.0.0.1:${PORT} (pace ${PACE})`));
