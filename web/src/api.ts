// Typed client for the control-plane API (docs/contracts.md section 4).
// Shapes come from the contract plus the Python code they wrap (runner/store.py rows,
// control/ops.py status, agents/interface.py events). Fields marked "optional extra" are
// not in the contract; the UI shows them when present and degrades when absent.

// ---------------------------------------------------------------- settings
export interface Settings {
  ai_enabled: boolean;
  agent: "scripted" | "deep";
  model: string;
}

// ---------------------------------------------------------------- recordings
export interface ElementInfo {
  tag: string | null;
  role: string | null;
  name: string | null;
  label: string | null;
  testid: string | null;
  css: string | null;
  nearby: string | null;
}

/** One captured event (schema = fixtures/recording.json `events[]`). */
export interface CaptureEvent {
  t: number;
  type: "framenavigated" | "net" | "click" | "copy" | "paste" | "input" | "change" | "submit" | string;
  url?: string;
  title?: string;
  method?: string;
  status?: number;
  req?: unknown;
  resp?: unknown;
  el?: ElementInfo;
  value?: string;
  text?: string;
}

export interface RecordingSummary {
  id: string;
  intent: string;
  allowlist: string[];
  status: "recording" | "stopped";
  started_at?: number | string | null;
  stopped_at?: number | string | null;
  n_events?: number;
  /** true for the built-in `fixture` recording (fixtures/recording.json), listed first */
  fixture?: boolean;
}

export interface TextMapping {
  dest_field: string;
  value: unknown;
  kind: "text";
  source_field: string;
  offset: [number, number];
  label?: string | null;
  candidate_regex?: string | null;
  cast?: string | null;
}
export interface JsonMapping {
  dest_field: string;
  value: unknown;
  kind: "json";
  json_path: string;
}
export type Mapping = TextMapping | JsonMapping;

export interface Candidates {
  recording_id: string;
  intent: string;
  sink: { base_url: string; method: string; path: string; body: Record<string, unknown> };
  source: {
    base_url: string;
    list: { method: string; path: string };
    record_id: string;
    text_field: string;
    record: Record<string, unknown>;
  };
  mappings: Mapping[];
  unexplained: { dest_field: string; value: unknown }[];
}

export interface Recording extends RecordingSummary {
  events: CaptureEvent[];
  candidates: Candidates | null;
}

// ---------------------------------------------------------------- agents
export type ThreadState = "running" | "waiting" | "done" | "failed";
export type TodoStatus = "pending" | "in_progress" | "completed";
export interface Todo {
  content: string;
  status: TodoStatus;
}
export interface TestsResult {
  passed: boolean;
  summary: string;
}
export type JsonPatchOp = { op: string; path: string; from?: string; value?: unknown };

interface EvBase {
  seq: number;
  t: string;
  thread_id: string;
}
/** agents.interface.EVENT_TYPES, one variant per type. */
export type AgentEvent = EvBase &
  (
    | { type: "status"; state: ThreadState; mode: "build" | "repair" }
    | { type: "todos"; todos: Todo[] }
    | { type: "subagent_start"; name: string; task: string }
    | { type: "subagent_end"; name: string; summary: string }
    | { type: "tool_call"; agent: string; tool: string; args: Record<string, unknown> }
    | { type: "tool_result"; agent: string; tool: string; ok: boolean; summary: string }
    | { type: "message"; agent: string; text: string }
    | { type: "artifact"; kind: "recording" | "candidates" | "spec" | "package"; name: string; version: number | null; path: string }
    | { type: "ask_user"; interrupt_id: string; question: string; options: string[] }
    | {
        type: "request_deploy";
        interrupt_id: string;
        name: string;
        version: number;
        summary: string;
        spec_diff: string;
        code_diff: string;
        tests: TestsResult;
        spec_patch?: JsonPatchOp[];
      }
    | { type: "resumed"; interrupt_id: string; payload: Record<string, unknown> }
    | { type: "deployed"; name: string; version: number; previous_version: number | null }
    | { type: "done"; result: unknown }
    | { type: "error"; message: string }
  );
export type AgentEventType = AgentEvent["type"];
export const AGENT_EVENT_TYPES: AgentEventType[] = [
  "status", "todos", "subagent_start", "subagent_end", "tool_call", "tool_result", "message",
  "artifact", "ask_user", "request_deploy", "resumed", "deployed", "done", "error",
];
export type InterruptEvent = Extract<AgentEvent, { type: "ask_user" | "request_deploy" }>;

export interface ThreadStatus {
  thread_id: string;
  mode?: "build" | "repair";
  subject?: string;
  state: ThreadState;
  pending: InterruptEvent | null;
  last_seq: number;
  created_at?: string;
}

export type ResumeBody =
  | { interrupt_id: string; answer: string }
  | { interrupt_id: string; decision: "approve" | "reject"; comment?: string };

// ---------------------------------------------------------------- integrations
export interface IntegrationSummary {
  name: string;
  active_version: number | null;
  latest_version: number;
  open_incidents: number | IncidentSummary[];
}
export interface VersionRow {
  version: number;
  tests_passed: boolean | number;
  tests_summary: string | null;
  created_at: string;
  deployed_at: string | null;
}
export type Counts = Record<string, number>;
export interface RunRow {
  id: number;
  integration?: string;
  version: number;
  kind?: "run" | "replay";
  dry_run: boolean;
  /** records fetched; null until known (or for runs not started by this API process) */
  total?: number | null;
  status: "running" | "completed" | "blocked" | "tripped" | "failed" | string;
  started_at?: string;
  finished_at?: string | null;
  duration_s: number | null;
  counts: Counts;
  llm_calls: number;
  incident_id: number | null;
  message?: string | null;
}
/** control.ops.status */
export interface IntegrationStatus {
  name: string;
  active_version: number | null;
  versions: VersionRow[];
  recent_runs: RunRow[];
  open_incidents: IncidentSummary[];
}
export interface TestCase {
  file: string;
  name: string;
  status: "PASSED" | "FAILED" | "SKIPPED" | "ERROR" | string;
}
export interface VersionDetail {
  name?: string;
  active?: boolean;
  created_at?: string;
  deployed_at?: string | null;
  version: number;
  manifest: Record<string, unknown>;
  spec_yaml: string;
  files: Record<string, string>;
  tests_passed: boolean;
  tests_summary: string | null;
  /** optional extra (mock only today): per-test results; the real API has tests_summary and tests/ files */
  tests?: TestCase[];
}
export interface VersionDiff {
  name?: string;
  from?: number;
  to?: number;
  spec_patch: JsonPatchOp[];
  spec_diff: string;
  code_diff: Record<string, string>;
}

// ---------------------------------------------------------------- runs & incidents
export interface RecordRow {
  run_id: number;
  record_id: string;
  status: "success" | "duplicate" | "quarantined";
  reason: string | null;
  detail: string | null;
  parser: string | null;
  route_value: string | null;
  dedupe_key: string | null;
  sink_body: Record<string, unknown> | null;
  raw: Record<string, unknown>;
}
export interface IncidentSummary {
  id: number;
  integration: string;
  version: number;
  run_id: number | null;
  type: "CONTRACT_CHANGED" | "CIRCUIT_BREAKER" | "NEW_SOURCE_FORMAT" | string;
  reason_codes: string[];
  summary: string;
  status: "open" | string;
  created_at: string;
}
export interface Incident extends IncidentSummary {
  detail: { contract_errors?: string[]; sink_call?: { method: string; path: string } } & Record<string, unknown> | null;
  samples: unknown[] | null;
  old_openapi: unknown;
  new_openapi: unknown;
}
export interface RunDetail {
  run: RunRow;
  records: RecordRow[];
  incident: Incident | null;
}
/** Run stream `progress` data: a snapshot, sent whenever it changes (SSE id = processed). */
export interface RunProgress {
  run_id?: number;
  status?: string;
  counts: Counts;
  processed: number;
  total?: number | null;
}
/** Run stream `finished` data (flat; older drafts wrapped it as {summary}). */
export interface RunSummary {
  run_id: number;
  integration: string;
  version: number;
  kind?: "run" | "replay";
  dry_run: boolean;
  status: string;
  counts: Counts;
  duration_s: number;
  llm_calls: number;
  incident_id: number | null;
  message: string | null;
}
export interface Accuracy {
  records: number;
  correct: number;
  accuracy: number | null;
  mismatches: unknown[];
  integration?: string | null;
  truth_source?: "inbox" | "seed";
}
// ---------------------------------------------------------------- benchmark (bench/results.json)
/** How a number was obtained. Anything but "measured" must look different in the UI. */
export type BenchKind = "measured" | "measured (earlier run)" | "extrapolated" | "estimate" | "not measured";
export interface Metric {
  value: number | null;
  /** s | ms | min | records | calls | ratio (0..1) | code */
  unit: string;
  kind: BenchKind;
  source: string;
  note?: string;
}
export interface BreakResult {
  name: string;
  records_in_break: number;
  /** flagged THIS break specifically (not just generic noise) */
  detected: boolean;
  caught_before_bad_write: boolean;
  bad_writes: Metric;
  silently_dropped: Metric;
  held: Metric;
  time_to_detect_s: Metric;
  repair_s: Metric;
  incident_type: string | null;
  alert: string;
  outcome: string;
}
export interface BenchBreaks {
  break1: BreakResult;
  break2: BreakResult;
}
export interface BenchResults {
  schema_version: 1;
  generated_at: string;
  command: string;
  scenario: { seed_records: number; run_records: number; break1: string; break2: string; notes: string[] };
  patchwork: {
    agent: string;
    build: { wall_s: Metric; llm_calls: Metric };
    run: { records: Metric; wall_s: Metric; per_record_ms: Metric; llm_calls: Metric; accuracy: Metric };
    seed_accuracy: Metric;
    setup_human_min: Metric;
    breaks: BenchBreaks;
    deep_agent: {
      date: string; model: string; source: string;
      build_s: Metric; build_calls: Metric; repair_break1_s: Metric; repair_break1_calls: Metric;
      repair_break2_s: Metric; repair_break2_calls: Metric; demo_total_s: Metric;
    };
  };
  baseline_script: {
    prompt: string; script: string; how_written: string;
    run: {
      records: Metric; wall_s: Metric; per_record_ms: Metric; llm_calls: Metric; correct_writes: Metric;
      wrong_writes: Metric; silently_dropped: Metric; held: Metric; accuracy: Metric; exit_code: Metric;
    };
    steady_state_exit_code: Metric;
    breaks: BenchBreaks;
  };
  manual: { per_record_min: Metric; timings_s: number[] };
  computer_use: { per_record_min: Metric; setup_min: Metric; llm_calls_per_record: Metric; timings_s: number[] };
  summary: { breaks_total: number; caught_before_bad_write: { patchwork: number; baseline_script: number } };
}

export interface DeployResult {
  name: string;
  active_version: number;
  previous_version: number | null;
  /** deploy resolves open incidents raised on older versions */
  resolved_incidents?: number[];
  rolled_back?: boolean;
}

// ---------------------------------------------------------------- HTTP
export class ApiError extends Error {
  constructor(public status: number, public detail: string) {
    super(detail);
  }
}

let base = "";
/** Prefix for every request (default: same origin, proxied by Vite). Tests point this at a fake. */
export function setApiBase(url: string) {
  base = url.replace(/\/$/, "");
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(base + path, {
    method,
    headers: body === undefined ? undefined : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!res.ok) {
    const detail = (data && typeof data === "object" && "detail" in data ? String((data as { detail: unknown }).detail) : text) || res.statusText;
    throw new ApiError(res.status, detail);
  }
  return data as T;
}

const qs = (params: Record<string, string | number | boolean | undefined | null>) => {
  const p = Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "");
  return p.length ? "?" + p.map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`).join("&") : "";
};
const enc = encodeURIComponent;

export const api = {
  health: () => request<unknown>("GET", "/api/health"),
  settings: () => request<Settings>("GET", "/api/settings"),
  putSettings: (s: { ai_enabled: boolean }) => request<Settings>("PUT", "/api/settings", s),

  startRecording: (b: { intent: string; allowlist: string[]; start_url?: string }) =>
    request<{ id: string }>("POST", "/api/recordings", b),
  stopRecording: (id: string) => request<RecordingSummary>("POST", `/api/recordings/${enc(id)}/stop`),
  recordings: () => request<RecordingSummary[]>("GET", "/api/recordings"),
  recording: (id: string) => request<Recording>("GET", `/api/recordings/${enc(id)}`),
  findCandidates: (id: string) => request<Candidates>("POST", `/api/recordings/${enc(id)}/candidates`),

  build: (recording_id: string) => request<{ thread_id: string }>("POST", "/api/integrations/build", { recording_id }),
  repair: (incident_id: number) => request<{ thread_id: string }>("POST", `/api/incidents/${incident_id}/repair`),
  threads: () => request<ThreadStatus[]>("GET", "/api/agent/threads"),
  thread: (id: string) => request<ThreadStatus>("GET", `/api/agent/${enc(id)}`),
  resume: (id: string, body: ResumeBody) => request<ThreadStatus>("POST", `/api/agent/${enc(id)}/resume`, body),

  integrations: () => request<IntegrationSummary[]>("GET", "/api/integrations"),
  integration: (name: string) => request<IntegrationStatus>("GET", `/api/integrations/${enc(name)}`),
  version: (name: string, v: number) => request<VersionDetail>("GET", `/api/integrations/${enc(name)}/versions/${v}`),
  diff: (name: string, from: number, to: number) =>
    request<VersionDiff>("GET", `/api/integrations/${enc(name)}/diff${qs({ from, to })}`),
  deploy: (name: string, version: number) => request<DeployResult>("POST", `/api/integrations/${enc(name)}/deploy`, { version }),
  rollback: (name: string, version: number) => request<DeployResult>("POST", `/api/integrations/${enc(name)}/rollback`, { version }),
  replay: (name: string) => request<{ run_id: number }>("POST", `/api/integrations/${enc(name)}/replay`),

  startRun: (b: { integration: string; dry_run?: boolean; limit?: number; version?: number }) => request<{ run_id: number }>("POST", "/api/runs", b),
  runs: (p: { integration?: string; limit?: number } = {}) => request<RunRow[]>("GET", `/api/runs${qs(p)}`),
  run: (id: number) => request<RunDetail>("GET", `/api/runs/${id}`),
  incidents: (status?: "open") => request<IncidentSummary[]>("GET", `/api/incidents${qs({ status })}`),
  incident: (id: number) => request<Incident>("GET", `/api/incidents/${id}`),

  demoReset: () => request<unknown>("POST", "/api/demo/reset"),
  stockroomSchema: (version: number) => request<unknown>("POST", "/api/demo/stockroom/schema", { version }),
  deliver: (count: number) => request<unknown>("POST", "/api/demo/inbox/deliver", { count }),
  newSupplier: (count: number) => request<unknown>("POST", "/api/demo/inbox/new_supplier", { count }),
  accuracy: (integration: string) => request<Accuracy>("GET", `/api/bench/accuracy${qs({ integration })}`),
  /** bench/results.json as written by `make bench`; 404 until it has been run. */
  benchResults: () => request<BenchResults>("GET", "/api/bench/results"),
};

// ---------------------------------------------------------------- SSE
export interface SseMessage<T = unknown> {
  id: number | null;
  event: string;
  data: T;
}
export interface StreamOptions<T> {
  /** Named SSE events to listen for (EventSource only delivers named events you subscribe to). */
  events: readonly string[];
  /** Resume after this id (sent as ?after=; native reconnects send Last-Event-ID). */
  after?: number;
  onMessage: (m: SseMessage<T>) => void;
  /** Return true to close the stream for good (e.g. "finished", thread "done"). */
  isTerminal?: (m: SseMessage<T>) => boolean;
  onStateChange?: (s: "open" | "reconnecting" | "closed") => void;
  /** For tests. */
  EventSourceImpl?: typeof EventSource;
  /** Snapshot streams (runs) reuse ids, so don't drop events whose id was already seen. */
  snapshots?: boolean;
  maxBackoffMs?: number;
}
export interface StreamHandle {
  close(): void;
  lastId(): number;
}

/** Build a stream URL with ?after=N, keeping any existing query. */
export function withAfter(url: string, after: number): string {
  if (!after) return url;
  return url + (url.includes("?") ? "&" : "?") + "after=" + after;
}

/**
 * Subscribe to an SSE endpoint and survive drops. Tracks the last `id:` seen; if the
 * connection dies the browser reconnects natively with Last-Event-ID, and if it gives up
 * (readyState CLOSED, e.g. the server ended the response) we reopen with ?after=<last id>.
 * Events with an id at or below the last one are dropped, so reconnects never double-apply.
 */
export function openStream<T = unknown>(url: string, opts: StreamOptions<T>): StreamHandle {
  const ES = opts.EventSourceImpl ?? EventSource;
  let last = opts.after ?? 0;
  let closed = false;
  let es: EventSource | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let backoff = 250;

  const close = () => {
    closed = true;
    if (timer) clearTimeout(timer);
    es?.close();
    opts.onStateChange?.("closed");
  };

  const handler = (ev: MessageEvent) => {
    // A refused or dropped connection fires a data-less "error" Event, which also reaches a listener
    // for a server event named "error" (the agent stream has one). Only real messages carry data.
    if (typeof ev.data !== "string") return;
    const id = ev.lastEventId ? Number(ev.lastEventId) : null;
    if (id !== null && !Number.isNaN(id)) {
      if (id <= last && !opts.snapshots) return;
      last = Math.max(last, id);
    }
    let data: T;
    try {
      data = JSON.parse(ev.data) as T;
    } catch {
      data = ev.data as unknown as T;
    }
    const msg: SseMessage<T> = { id, event: ev.type, data };
    backoff = 250;
    opts.onMessage(msg);
    if (opts.isTerminal?.(msg)) close();
  };

  const connect = () => {
    if (closed) return;
    es = new ES(withAfter(url, last));
    es.onopen = () => opts.onStateChange?.("open");
    for (const name of opts.events) es.addEventListener(name, handler as EventListener);
    es.onerror = () => {
      if (closed) return;
      opts.onStateChange?.("reconnecting");
      if (es && es.readyState === 2 /* CLOSED */) {
        es.close();
        timer = setTimeout(connect, backoff);
        backoff = Math.min(backoff * 2, opts.maxBackoffMs ?? 5000);
      }
    };
  };
  connect();
  return { close, lastId: () => last };
}

export const streams = {
  recording: (id: string, o: Omit<StreamOptions<CaptureEvent | RecordingSummary>, "events">) =>
    openStream(`${base}/api/recordings/${enc(id)}/stream`, { events: ["capture", "stopped"], ...o }),
  agent: (threadId: string, o: Omit<StreamOptions<AgentEvent>, "events">) =>
    openStream(`${base}/api/agent/${enc(threadId)}/stream`, { events: AGENT_EVENT_TYPES, ...o }),
  run: (runId: number, o: Omit<StreamOptions<RunProgress | { summary: RunSummary } | RunSummary>, "events">) =>
    openStream(`${base}/api/runs/${runId}/stream`, { events: ["progress", "finished"], snapshots: true, ...o }),
};

// ---------------------------------------------------------------- helpers shared by views
/** Run counts use keys "success", "duplicate", "quarantined" and "quarantined:<REASON>". */
export function splitCounts(c: Counts) {
  const reasons: Record<string, number> = {};
  for (const [k, v] of Object.entries(c)) if (k.startsWith("quarantined:")) reasons[k.slice(12)] = v;
  return { success: c.success ?? 0, duplicate: c.duplicate ?? 0, quarantined: c.quarantined ?? 0, reasons };
}

/** The "finished" payload is {summary} per the contract; accept a bare summary too. */
export function finishedSummary(d: { summary: RunSummary } | RunSummary): RunSummary {
  return "summary" in d && d.summary && typeof d.summary === "object" ? d.summary : (d as RunSummary);
}
