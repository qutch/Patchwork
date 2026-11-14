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

## 5. UI (owned by UI)

Vite + React + TypeScript + Tailwind in `web/`; dev server proxies `/api` to the control plane. Five tabs from plan section 11:
**Record · Review · Agent Activity · Integration · Runs**, plus a header with the AI on/off toggle and a global `LLM calls: 0` badge on Runs.
Build against `PATCHWORK_AGENT=scripted` (no model needed); everything must also work with `deep`.
