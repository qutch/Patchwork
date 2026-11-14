# Phase 2 contracts

The four workstreams build in parallel against these interfaces. Change one only by
updating this file in the same commit, and say so in your report.

| Workstream | Owns (write access) | Must not edit |
|---|---|---|
| **Capture** (D1) | `capture/`, `matcher/`, `demo_apps/`, `tests/capture/`, `tests/matcher/`, `tests/demo_apps/` | `agents/`, `control/`, `web/` |
| **Agents** (D3) | `agents/` (keep `interface.py` compatible), `tests/agents/`, `fixtures/agent_*` | `control/`, `web/`, `capture/` |
| **Control plane** (D4a) | `control/` (incl. new `control/api/`), `runner/` (small, test-covered changes only), `bench/`, `tests/control/` | `agents/`, `web/`, `capture/` |
| **UI** (D4b) | `web/` | everything else |

Shared files (`pyproject.toml`, `Makefile`, `README.md`, `ROADMAP.md`, `.gitignore`): append only, keep edits small; the integrator merges them.

**Ports** (never use 8001–8003 or 8000 in tests; the integrator runs the demo there):
capture 18000–18999 · agents 28000–28999 · control plane 38000–38999 · UI dev server 5180 (proxy to a control plane on 38080).
Tests should use free ports (see `tests/conftest.py::apps`).

**Environment**: Python venv at the integration worktree, already has every dependency
(`deepagents 0.7.38`, `langchain-openrouter 0.2.9`, `langgraph 1.2.22`,
`langgraph-checkpoint-sqlite 3.1.3`, `playwright 1.65.2` + Chromium, `jsonpatch 1.47`, FastAPI, httpx, pytest).
`.env` (gitignored) holds `OPENROUTER_API_KEY`, `PATCHWORK_MODEL`, `PATCHWORK_MODEL_FALLBACK`.
The runner's environment never contains model keys (`control.ops.runner_env` strips them).

---

## 1. Workspace layout (`$PATCHWORK_WORKSPACE`, default `./workspace`)

```
patchwork.db                                SQLite (runner/store.py)
recordings/<recording_id>/events.jsonl      live, one captured event per line (append-only)
recordings/<recording_id>/recording.json    final, written on stop (schema = fixtures/recording.json)
recordings/<recording_id>/candidates.json   matcher output
integrations/<name>/work/                   the agent's filesystem root: spec.yaml, examples.yaml, transforms.py
integrations/<name>/v<N>/                   compiled, immutable packages
agent_threads/<thread_id>.jsonl             agent event log
runs/<run_id>.jsonl                         runner log
```

`recording_id` = `rec-<yyyymmdd-hhmmss>-<4 hex>`. The special id `fixture` means `fixtures/recording.json`.

## 2. Capture (owned by Capture)

```python
# capture/session.py
class RecordingSession:
    id: str
    dir: Path                          # workspace/recordings/<id>
    def events(self, after: int = 0) -> list[dict]   # live; reads events.jsonl, 1-based index = line number
    def stop(self) -> dict             # writes recording.json, returns it; idempotent
    @property
    def running(self) -> bool

def start_recording(intent: str, allowlist: list[str], start_url: str | None = None,
                    headless: bool = False) -> RecordingSession     # launches a subprocess
def get_session(recording_id: str) -> RecordingSession | None      # running or finished
def list_recordings() -> list[dict]   # [{id, intent, allowlist, status: recording|stopped, started_at, stopped_at, n_events}]
```

Under it: `python -m capture record <dir> --intent ... --allow host:port ... [--start-url URL] [--headless]`
(Playwright Chromium, `add_init_script(recorder.js)`, `expose_binding('pwRecord')`, `page.on('request'/'response')`).
Event shape, redaction and allowlist rules: plan section 5 and `fixtures/recording.json`.

```python
# matcher/__init__.py
def match(recording: dict) -> dict   # pure, no network, no LLM
```
`python -m matcher <recording.json>` prints candidates JSON. **candidates.json**:
```json
{
  "recording_id": "rec-…",
  "intent": "…",
  "sink":   {"base_url": "http://localhost:8002", "method": "POST", "path": "/api/incoming", "body": {"product_id": "GAD-217", "quantity": 20, "reference": "PO-1022"}},
  "source": {"base_url": "http://localhost:8001", "list": {"method": "GET", "path": "/api/messages"},
             "record_id": "id", "text_field": "body", "record": {"id": "msg-0007", "body": "…"}},
  "mappings": [
    {"dest_field": "quantity", "value": 20, "kind": "text", "source_field": "body",
     "offset": [83, 85], "label": "Qty:", "candidate_regex": "Qty:\\s*(\\d+)", "cast": "int"},
    {"dest_field": "x", "value": "…", "kind": "json", "json_path": "$.subject"}
  ],
  "unexplained": [{"dest_field": "warehouse_id", "value": "BOS"}]
}
```

Demo-app additions (Capture owns `demo_apps/`):
- Inbox `POST /admin/deliver {"count": N}` appends N new deterministic messages (same 3 formats, same mix ratio), `received_at` after the latest. `POST /admin/reset` restores the 150-message seed.
- Inbox `POST /admin/new_supplier {"count": N}` delivers N messages in the Break 2 format from `umbrella.com`: `Item code: WID-104 / 2 cases (12/case)` + a PO reference. Ground truth for both endpoints is exposed at `GET /admin/ground_truth`.

**Additions made while building Capture (all additive; nothing above was removed or renamed):**
- Recording dir also holds `meta.json` (id, intent, allowlist, start_url, status, started_at, stopped_at, pid), `recorder.log` and, while stopping, a `STOP` file. `recording.json` also carries `start_url`. The recorder writes `candidates.json` itself on stop. `POST /api/recordings/{id}/candidates` can still re-run the matcher.
- `RecordingSession` also has `meta()` and `summary()`. `summary()` returns the `list_recordings()` row. `stop()` blocks for up to 20 s while the recorder flushes, then kills the recorder and finalizes from `events.jsonl` itself.
- Event extras:
  - net events carry `origin` (`http://host:port`), and may carry `req_headers` (secret and browser-noise headers removed), `resp_truncated` / `req_truncated` (the body is then a string cut at 20 KB), `resp_type` (a non-text body is not stored) or `error` (failed request).
  - keydown events carry `key: "Enter"`.
  - Element descriptors add `id` and `accessible_name`. `name` is the `name` attribute for form controls, otherwise the accessible name, as in the fixture.
- Redaction also masks values in JSON/form bodies under secret-looking keys (`password`, `token`, `api_key`, `otp`, `cvv`, `card_number`…). It also masks keys whose form field on the page was a password/cc-*/one-time-code field, and drops `X-Api-Key` / `X-CSRF-Token`.
- candidates.json extras:
  - `sink.status`
  - `source.detail` (`{"method": "GET", "path": "/api/messages/{id}"}`) when the record came from a detail GET
  - `cast` is always present (`null` for strings)
  - text mappings add `matched` (source text), `regex_ok` (the regex re-extracts the value), optional `copied: true` and `alternatives` (other places the value appeared)
  - `kind: "copy"` (`{text}`) when only copied text explains a value
  - with no mutating request captured, `sink` is `null` and typed input values are the destination values
- Ground truth rows for delivered messages add `delivered: true`. Rows for new-supplier messages add `new_supplier: true` and `intended: {body, body_v2, cases, per_case}`, which is what a repaired spec should send. Score delivered runs with `bench.accuracy.score(records, truth=<GET /admin/ground_truth>)`. The default truth covers only the 150-message seed.
- The Inbox HTML pages now render from `GET /api/messages[/{id}]` with `fetch`, so a recording sees the source record. The JSON API is unchanged.

## 3. Agents (owned by Agents)

`agents/interface.py` is the contract (already implemented and tested): `AgentService` with
`start_build(recording_id) -> thread_id`, `start_repair(incident_id) -> thread_id`,
`events(thread_id, after=0)`, `status(thread_id)`, `resume(thread_id, interrupt_id, payload)`, `list_threads()`.
`agents.get_agent_service()` picks the implementation from `PATCHWORK_AGENT=scripted|deep`.

Event types and payload keys: `agents.interface.EVENT_TYPES`. Every event has `seq` (1-based per thread), `t`, `thread_id`, `type`.
Resume payloads: `ask_user` → `{"answer": "<one of options or free text>"}`; `request_deploy` → `{"decision": "approve"|"reject", "comment": "…"}`.
`request_deploy` events carry `name, version, summary, spec_diff (unified), code_diff (unified), tests {passed, summary}`, and for repairs `spec_patch` (JSON Patch).
On approve the agent itself calls `control.ops.deploy(name, version)` and emits `deployed`; in REPAIR mode it then calls `control.ops.replay_quarantined(name)` if present.

## 4. Control-plane API (owned by Control plane; consumed by UI)

FastAPI app `control.api:app`, run with `python -m control.api --port 38080`. JSON everywhere; errors are `{"detail": "…"}` with 4xx.
SSE endpoints send `id: <seq>` + `event: <type>` + `data: <json>` and honour `Last-Event-ID` / `?after=`.

```
GET  /api/health
GET  /api/settings                                   {"ai_enabled": bool, "agent": "scripted"|"deep", "model": str}
PUT  /api/settings {"ai_enabled": bool}

POST /api/recordings {intent, allowlist[], start_url?}   -> {id}          (launches the browser)
POST /api/recordings/{id}/stop                       -> recording summary
GET  /api/recordings                                 -> list
GET  /api/recordings/{id}                            -> {id, intent, allowlist, status, events[], candidates|null}
GET  /api/recordings/{id}/stream                     SSE: event "capture" per captured event; "stopped" at end
POST /api/recordings/{id}/candidates                 -> candidates (runs the matcher)

POST /api/integrations/build {recording_id}          -> {thread_id}   409 if AI is off
POST /api/incidents/{id}/repair                      -> {thread_id}   409 if AI is off
GET  /api/agent/threads                              -> list of status
GET  /api/agent/{thread_id}                          -> status (incl. pending interrupt)
GET  /api/agent/{thread_id}/stream?after=N           SSE: agent events
POST /api/agent/{thread_id}/resume {interrupt_id, answer? | decision?, comment?}

GET  /api/integrations                               -> [{name, active_version, latest_version, open_incidents}]
GET  /api/integrations/{name}                        -> control.ops.status
GET  /api/integrations/{name}/versions/{v}           -> {version, manifest, spec_yaml, files: {relpath: text}, tests_passed, tests_summary}
GET  /api/integrations/{name}/diff?from=1&to=2       -> {spec_patch (JSON Patch), spec_diff, code_diff: {file: unified}}
POST /api/integrations/{name}/deploy {version}       -> deploy (human approval from the UI); 409 if tests not passed
POST /api/integrations/{name}/rollback {version}
POST /api/integrations/{name}/replay                 -> {run_id}   replays quarantined records on the active version

POST /api/runs {integration, dry_run?, limit?}       -> {run_id}   (background; does NOT need AI on)
GET  /api/runs?integration=&limit=
GET  /api/runs/{id}                                  -> {run, records: [...], incident|null}
GET  /api/runs/{id}/stream                           SSE: "progress" {counts, processed} ... "finished" {summary}
GET  /api/incidents?status=open
GET  /api/incidents/{id}

POST /api/demo/reset                                 reset Inbox, Stockroom live+staging, workspace state
POST /api/demo/stockroom/schema {version}            Break 1 switch (live + staging)
POST /api/demo/inbox/deliver {count}
POST /api/demo/inbox/new_supplier {count}            Break 2
GET  /api/bench/accuracy?integration=                exact accuracy vs ground truth
```

`control.ops` additions (Control plane implements; Agents may call): `replay_quarantined(name) -> run summary`,
`diff_versions(name, a, b) -> dict`, `rollback(name, version)`.
Runs must show live progress: the runner prints `{"event": "started", "run_id": N}` as its first stdout line
and the API streams counts while the subprocess runs. Optional `PATCHWORK_RUN_PACE_MS` slows the runner per record for a watchable demo.

**Details fixed while building the control plane (additive; nothing above was removed or renamed):**
- Errors: 404 unknown thing; 409 conflict (AI off, no active version, run already in progress for that
  integration, no pending interrupt, deploy refused, recording still running); 422 bad body; 501 capture/matcher
  missing; 502 demo app unreachable; 500 runner failed before printing `started`.
- `GET /api/health` -> `{status: "ok", workspace, agent, ai_enabled}`. Settings persist in `$PATCHWORK_WORKSPACE/settings.json`
  (kept by demo reset). `model` is `"scripted"` for the scripted agent, else `$PATCHWORK_MODEL`.
- Recordings: id `fixture` (fixtures/recording.json) always works, even without capture, and is listed first with
  `fixture: true`. List rows = `capture.session.list_recordings()` rows. `GET /api/recordings/{id}` also has
  `started_at, stopped_at, n_events`. Stream: `capture` events with `id` = 1-based event index, then `stopped`
  (data = list row). `POST /api/recordings` with an empty allowlist -> 422. `PATCHWORK_CAPTURE_HEADLESS=1` records headless.
- `POST /api/integrations/build`: 404 unknown recording, 409 still recording.
- Agent stream: `id` = `seq`, `event` = event `type`, `data` = the whole event. Closes after the thread is `done`/`failed`
  (a finished thread replays everything, then closes); keep-alive comments every 15 s.
- `POST /api/agent/{id}/resume` -> the thread status after resuming. For a pending `request_deploy` a `decision` is
  required (422 otherwise); for `ask_user` send `answer`.
- `GET /api/integrations` -> `open_incidents` is a **count**. `GET /api/integrations/{name}` (`control.ops.status`)
  keeps `open_incidents` as the list.
- `GET .../versions/{v}` also has `name, active, created_at, deployed_at`. `files` = every text file in the package
  (incl. `tests/`, `golden.json`, `contract/openapi.json`), no `__pycache__`.
- `GET .../diff`: `to` defaults to the latest version, `from` to `to - 1`. Response also has `name, from, to`.
  `spec_patch` is made by `jsonpatch.make_patch`, so renames show up as `move` ops. `code_diff` only lists files that
  differ, labelled `v{from}/{file}` -> `v{to}/{file}`.
- `deploy` -> `{name, active_version, previous_version, resolved_incidents: [ids]}`: deploying a version resolves open
  incidents raised on older versions (incident `status` becomes `resolved`). `rollback` must target an older
  version (409 otherwise), never resolves incidents, and adds `rolled_back: true`.
- `POST /api/runs` also takes `version` (default: active). Run rows (list and `GET /api/runs/{id}`.run) carry
  `kind: "run"|"replay"`, `dry_run` as a bool and `total` (records fetched; null until known or for runs not started by
  this API process). While running, `counts` are live.
- Run stream: `progress` data = `{run_id, status, counts, processed, total}`, sent whenever it changes; `finished` data =
  `{run_id, integration, version, dry_run, kind, status, counts, duration_s, llm_calls, incident_id, message}`.
  Progress events are snapshots, so `Last-Event-ID` is accepted but a reconnect just gets the current snapshot; ids are the processed count.
  A runner that dies mid-run is marked `failed` with its stderr tail in `message`.
- `GET /api/incidents`: `status` omitted = all; also `?integration=`.
- Demo proxies pass the app's status and JSON through. `POST /api/demo/stockroom/schema` -> `{stockroom: {...}, staging: {...}}`.
  `POST /api/demo/reset` -> `{ok, apps: {inbox, stockroom, staging: {status}}, removed: [...]}`; kills runs in progress,
  keeps recordings and settings.
- `GET /api/bench/accuracy` -> `bench.accuracy.score` output plus `integration, truth_source: "inbox"|"seed"`. It scores
  the latest live outcome per record (dry runs excluded); `integration` omitted = all integrations.
- Runner: stdout is JSON lines: `started`, then `{"event": "fetched", "run_id", "total"}` (absent if preflight blocks), then the
  summary (also has `kind`). `python -m runner run <name> --replay-quarantined` takes records from the DB.

## 5. UI (owned by UI)

Vite + React + TypeScript + Tailwind in `web/`; dev server proxies `/api` to the control plane. Five tabs from plan section 11:
**Record · Review · Agent Activity · Integration · Runs**, plus a header with the AI on/off toggle and a global `LLM calls: 0` badge on Runs.
Build against `PATCHWORK_AGENT=scripted` (no model needed); everything must also work with `deep`.
