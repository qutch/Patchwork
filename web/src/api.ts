// Typed client for the control-plane API (docs/contracts.md section 4).
// Shapes come from the contract plus the Python code they wrap (runner/store.py rows,
// control/ops.py status, agents/interface.py events). Fields marked "optional extra" are
// not in the contract; the UI shows them when present and degrades when absent.

// ---------------------------------------------------------------- settings
export interface Settings {
  ai_enabled: boolean;
  agent: "scripted" | "deep";
  model: string;
  /** v2 (V9): use a learned reader as soon as every code check passes ("auto", default), or always ask a person. */
  learn_policy?: LearnPolicy;
}
export type LearnPolicy = "auto" | "approve";

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
    | { type: "status"; state: ThreadState; mode: ThreadMode }
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
        /** teach from examples: one row per destination field (the review table) */
        review?: ReviewRow[];
      }
    | { type: "resumed"; interrupt_id: string; payload: Record<string, unknown> }
    | { type: "deployed"; name: string; version: number; previous_version: number | null }
    | { type: "done"; result: unknown }
    | { type: "error"; message: string }
    | { type: "evidence"; page_id: string; parser_id: string; fields: EvidenceField[]; ok: boolean }
    | {
        type: "learned";
        integration: string;
        parser_id: string;
        site: string;
        version: number | null;
        deployed: boolean;
        approval: "auto" | "human" | "pending";
        reason: string;
      }
  );
/** Task 5 `request_deploy.review[]`: where one destination field's value comes from, per example page. */
/** A source as the backend describes it: words, or a reader/record/today/const object (shown by `sourceLabel`). */
export type ReviewSource = string | Record<string, unknown>;
export interface ReviewRow {
  dest_field: string;
  /** a human label for the source, when the backend sends one (preferred over `source`) */
  label?: string;
  /** where the value comes from, e.g. "reader: h1.product-title", "the page's canonical URL", "today's date" */
  source: ReviewSource;
  /** example page id -> the value the source gives on that page */
  values: Record<string, string | null>;
  ai_proposed: boolean;
  status: "checked" | "needs_you";
  /** other sources the person may pick instead (the only ones the resume accepts besides a constant or skip) */
  alternatives: ReviewSource[];
}
/** Task 5 resume `edits[]`: `choice` is one of the row's `alternatives`, a constant or a skip. */
export interface ReviewEdit {
  dest_field: string;
  choice: ReviewSource | { const: string } | { skip: true };
}
export type ThreadMode = "build" | "repair" | "learn";
/** V10 `evidence.fields[]`: one page value the learner proposed, and what code found when it checked it. */
export interface EvidenceField {
  name: string;
  value: string | null;
  ok: boolean;
  where: "text" | "html" | "jsonld" | null;
  extractor: Record<string, unknown> | string | null;
}
export type AgentEventType = AgentEvent["type"];
export const AGENT_EVENT_TYPES: AgentEventType[] = [
  "status", "todos", "subagent_start", "subagent_end", "tool_call", "tool_result", "message",
  "artifact", "ask_user", "request_deploy", "resumed", "deployed", "done", "error", "evidence", "learned",
];
export type InterruptEvent = Extract<AgentEvent, { type: "ask_user" | "request_deploy" }>;

export interface ThreadStatus {
  thread_id: string;
  mode?: ThreadMode;
  subject?: string;
  state: ThreadState;
  pending: InterruptEvent | null;
  last_seq: number;
  created_at?: string;
  /** optional extra: the integration the thread works on, when the control plane says so */
  integration?: string;
}

export type ResumeBody =
  | { interrupt_id: string; answer: string }
  | { interrupt_id: string; decision: "approve" | "reject"; comment?: string; edits?: ReviewEdit[] };

// ---------------------------------------------------------------- integrations
/** Task 2: a draft is waiting for its examples, `teaching` while the agent builds it, `live` once deployed. */
export type IntegrationState = "draft" | "teaching" | "live";
export interface IntegrationSummary {
  name: string;
  /** Task 2; absent on older backends (read as live) */
  state?: IntegrationState;
  active_version: number | null;
  latest_version: number;
  open_incidents: number | IncidentSummary[];
  /** optional extra (v3): the spec's intent, when the control plane lists it */
  intent?: string | null;
  /** optional extra (v3): where it writes, when the control plane lists it (otherwise read from the active spec) */
  destination?: DestinationInfo | null;
  sink?: DestinationInfo | null;
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

// ---------------------------------------------------------------- v2: pages, tracker, layouts (contracts V1, V4, V9, V12)
export const TRACKER = "jobs_to_tracker";

/** V1 page record (`GET /api/pages`); `GET /api/pages/{id}` leaves out `html`. */
export interface PageRecord {
  id: string;
  seq: number;
  url: string;
  canonical_url: string;
  site: string;
  title: string;
  html?: string;
  text: string;
  captured_at: string;
  captured_date: string;
}
export interface PageAccepted {
  id: string;
  seq: number;
  site: string;
  canonical_url: string;
  integration: string | null;
  run_id: number | null;
  queued: boolean;
}
/** V4 parser provenance. */
export interface ParserOrigin {
  kind: "taught" | "learned" | "fixture";
  at: string | null;
  thread_id: string | null;
  approved_by: "auto" | "human" | null;
}
export type TrackerStatus = "pending" | "running" | "success" | "duplicate" | "held" | "learning" | "failed";
export type TrackerLabel = "code" | "learned_now" | "held" | "duplicate";
/** V9 TrackerRow: one hotkey press and what happened to it. */
export interface TrackerRow {
  page_id: string;
  seq: number;
  captured_at: string;
  site: string;
  url: string;
  title: string | null;
  company: string | null;
  status: TrackerStatus;
  label: TrackerLabel | null;
  reason: string | null;
  detail: string | null;
  parser: string | null;
  version: number | null;
  routed_by: "match" | "fingerprint" | "default" | null;
  parser_origin: ParserOrigin | null;
  ai_calls: number;
  run_id: number | null;
  learn_thread_id: string | null;
  incident_id: number | null;
  /** V15: every extracted field, spec order (absent on older backends: use title/company). */
  fields?: Record<string, unknown> | null;
}
export interface TrackerTotals {
  pages: number;
  ai_calls: number;
  code: number;
  learned_now: number;
  held: number;
  duplicate: number;
}
export interface TrackerResponse {
  rows: TrackerRow[];
  totals: TrackerTotals;
  /** V15: the integration's extracted field names, spec order (absent on older backends). */
  field_names?: string[] | null;
}
/** V9 Layout: one parser across the integration's versions. */
export interface Layout {
  integration: string;
  parser_id: string;
  site: string | null;
  fingerprint: string | null;
  fields: Record<string, unknown>;
  origin: ParserOrigin | null;
  introduced_in: number;
  active: boolean;
}
/** V15 item 2: one page integration the extension can send pages to. */
export interface PageIntegration {
  name: string;
  intent?: string | null;
  /** optional extra: where it writes, if the control plane says so */
  destination?: DestinationInfo | null;
  sink?: DestinationInfo | null;
}
export interface ExtensionInfo {
  token: string;
  control_url: string;
  /** V15 item 2 (absent on older backends: fall back to /api/integrations) */
  integrations?: PageIntegration[] | null;
  default_integration?: string | null;
}
/** Where an integration writes: its spec's `sink` (V5 form, V14 airtable, v1 api). Never carries a value. */
export interface DestinationInfo {
  kind: "form" | "airtable" | "api" | string;
  url?: string | null;
  base_url?: string | null;
  base_id?: string | null;
  table?: string | null;
  /** the credential NAME the sink's auth references (V13), never its value */
  credential?: string | null;
}
/** Task 3 destination per kind, as sent to `PUT /api/integrations/{name}/destination`. */
export type DestinationInput =
  | { kind: "airtable"; base_id?: string; base_url?: string; table: string; credential: string }
  | { kind: "form"; url: string }
  | { kind: "api"; base_url: string; operation: { method: string; path: string }; credential?: string }
  | { kind: "sqlite"; path: string; table: string };
/** Task 2 draft. */
export interface Draft {
  name: string;
  intent: string;
  state: IntegrationState;
  destination: (DestinationInput & Record<string, unknown>) | null;
  example_page_ids: string[];
  thread_id: string | null;
  created_at: string;
}
export interface DestinationField {
  name: string;
  type: string;
  required: boolean;
  options?: string[];
}
export interface ApiOperation {
  method: string;
  path: string;
  summary: string;
  fields: string[];
}
/** Task 3 check result: what the destination wants, by field. */
export interface DestinationCheck {
  kind: string;
  title: string;
  fields: DestinationField[];
  operations?: ApiOperation[];
}
/** V13 `GET /api/credentials`: names and whether they are set. Values are never sent. */
export interface Credential {
  name: string;
  set: boolean;
  used_by: string[];
}
/** V12 bench/v2/results.json. */
export interface BenchV2Posting {
  n: number;
  file: string;
  site: string;
  label: TrackerLabel | string | null;
  ai_calls: number;
  seconds: number | null;
  correct: boolean | null;
}
export interface BenchV2Number {
  name: string;
  value: number | null;
  unit: string;
  kind: BenchKind;
  source: string;
  note?: string;
}
export interface BenchV2Results {
  postings: BenchV2Posting[];
  series: {
    patchwork_cumulative_ai_calls?: number[];
    browser_agent_cumulative_ai_calls?: number[];
  };
  numbers: BenchV2Number[];
  generated_at?: string;
  /** optional extra: true when web/mock serves illustrative numbers (the UI badges them "mock data") */
  mock?: boolean;
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

async function request<T>(method: string, path: string, body?: unknown, extraHeaders?: Record<string, string>): Promise<T> {
  const headers: Record<string, string> = { ...(body === undefined ? {} : { "content-type": "application/json" }), ...extraHeaders };
  const res = await fetch(base + path, {
    method,
    headers: Object.keys(headers).length ? headers : undefined,
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
  putSettings: (s: { ai_enabled?: boolean; learn_policy?: LearnPolicy }) => request<Settings>("PUT", "/api/settings", s),

  startRecording: (b: { intent: string; allowlist: string[]; start_url?: string }) =>
    request<{ id: string }>("POST", "/api/recordings", b),
  stopRecording: (id: string) => request<RecordingSummary>("POST", `/api/recordings/${enc(id)}/stop`),
  recordings: () => request<RecordingSummary[]>("GET", "/api/recordings"),
  recording: (id: string) => request<Recording>("GET", `/api/recordings/${enc(id)}`),
  findCandidates: (id: string) => request<Candidates>("POST", `/api/recordings/${enc(id)}/candidates`),

  build: (recording_id: string, name?: string) =>
    request<{ thread_id: string }>("POST", "/api/integrations/build", name ? { recording_id, name } : { recording_id }),
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

  // v2 (contracts V9)
  /** What the extension sends on a hotkey press. The UI only uses it in tests and rehearsal tools. */
  postPage: (b: { url: string; title: string; html: string; integration?: string }, token: string) =>
    request<PageAccepted>("POST", "/api/pages", b, { "X-Patchwork-Token": token }),
  pages: (p: { since?: number; limit?: number; integration?: string } = {}) => request<PageRecord[]>("GET", `/api/pages${qs(p)}`),
  page: (id: string) => request<{ page: PageRecord; row: TrackerRow }>("GET", `/api/pages/${enc(id)}`),
  extension: () => request<ExtensionInfo>("GET", "/api/extension"),
  /** V13; 404 on backends without credentials. */
  credentials: () => request<Credential[]>("GET", "/api/credentials"),
  /** Task 1: 201 `{name, set: true, used_by}`. The value is sent once and never comes back. */
  addCredential: (name: string, value: string) => request<Credential>("POST", "/api/credentials", { name, value }),

  // new integration wizard (Tasks 2-4)
  newIntegration: (intent: string, name?: string) => request<Draft>("POST", "/api/integrations/new", name ? { intent, name } : { intent }),
  draft: (name: string) => request<Draft>("GET", `/api/integrations/${enc(name)}/draft`),
  deleteIntegration: (name: string, deleteRows: boolean) =>
    request<unknown>("DELETE", `/api/integrations/${enc(name)}${qs({ delete_rows: deleteRows ? "true" : "false" })}`),
  putDestination: (name: string, d: DestinationInput) => request<DestinationCheck>("PUT", `/api/integrations/${enc(name)}/destination`, d),
  airtableTables: (base_id: string, credential: string) =>
    request<{ id: string; name: string }[]>("GET", `/api/destinations/airtable/tables${qs({ base_id, credential })}`),
  apiOperations: (base_url: string, credential?: string) =>
    request<ApiOperation[]>("GET", `/api/destinations/api/operations${qs({ base_url, credential })}`),
  teach: (name: string, page_ids: string[]) => request<{ thread_id: string }>("POST", `/api/integrations/${enc(name)}/teach`, { page_ids }),
  tracker: (p: { integration?: string; limit?: number } = {}) =>
    request<TrackerResponse>("GET", `/api/tracker${qs({ integration: p.integration ?? TRACKER, limit: p.limit })}`),
  layouts: (integration: string = TRACKER) => request<Layout[]>("GET", `/api/layouts${qs({ integration })}`),
  revertLayout: (parser_id: string, integration: string = TRACKER) =>
    request<{ version: number; deployed: true }>("POST", `/api/layouts/${enc(parser_id)}/revert`, { integration }),
  formRename: (from: string, to: string) => request<unknown>("POST", "/api/demo/form/rename", { from, to }),
  /** V15 item 4: `integration` defaults to jobs_to_tracker on the server; only sent when given. */
  v2SaveBaseline: (integration?: string) => request<unknown>("POST", `/api/demo/v2/save-baseline${qs({ integration })}`),
  v2Reset: (integration?: string) => request<unknown>("POST", `/api/demo/v2/reset${qs({ integration })}`),
  /** bench/v2/results.json (W5); 404 until it exists. */
  benchV2: () => request<BenchV2Results>("GET", "/api/bench/v2"),
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
