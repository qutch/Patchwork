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
GET  /api/bench/results                              bench/results.json (`make bench`); 404 until it exists
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

### Run recovery on control-plane startup

Runs record the runner's `pid`. On startup the control plane reconciles every run still marked `running`:
if its runner process is gone (or the pid now belongs to something else) the run becomes `failed` with the
counts it reached and a message saying the control plane restarted; finished records and the cursor are kept,
so running again continues where it stopped. If the runner is still alive it is **adopted, not killed**
(killing mid-record could resend a write not yet marked processed): it blocks new runs of that integration
(409), streams progress as usual (`total` is null), and is marked `failed` only if it exits without writing
its own summary. `GET /api/health` reports `recovered_runs: {failed: [...], adopted: [...]}`.
`/api/demo/reset` and shutdown kill adopted runners too.

---

# Part 2: v2 contracts (jobs → tracker form)

Design: `docs/superpowers/specs/2026-11-14-patchwork-v2-design.md`. Plan: `docs/superpowers/plans/2026-11-14-patchwork-v2-plan.md`.
Everything here is **additive**: Part 1 stays true for v1 (`po_to_stockroom`), and v1 behaviour and tests must not change.
Change a v2 contract only by editing this file in the same commit, and say so in your report.

## V0. Ownership, ports, environment

| Workstream | Owns (write access) | Must not edit |
|---|---|---|
| **W1 Engine** | `spec/`, `compiler/`, `runner/`, `demo_apps/tracker_form/`, `fixtures/jobs/`, `tests/engine/`, and in `control/ops.py` only `fetch_contract` + its use in `compile_and_test` | `agents/`, `control/api/`, `web/`, `capture/` |
| **W2 Capture + extension** | `extension/`, `capture/`, `matcher/`, `tests/capture/`, `tests/matcher/`, `tests/extension/`, `fixtures/recording_jobs/` | `runner/`, `agents/`, `control/`, `web/` |
| **W3 Agents** | `agents/`, `tests/agents/`, `fixtures/agent_*` | `runner/`, `compiler/`, `control/`, `web/` |
| **W4a Control plane** | `control/` (except W1's `fetch_contract`), `tests/control/` | `runner/`, `agents/`, `web/` |
| **W4b UI** | `web/` (incl. `web/mock/`) | everything else |
| **W5 Proof** | `bench/v2/`, `fixtures/postings/`, `scripts/smoke_v2.py`, `docs/demo-v2.md`, `tests/bench/test_v2_*.py` | everything else (v1 `bench/` files: append only) |

`fixtures/pages/` (8 rendered pages + `index.json`, from spike b) is shared and read-only. Shared files (`pyproject.toml`,
`Makefile`, `README.md`, `ROADMAP.md`, `.gitignore`): append only, small; the integrator merges them.

**Never** bind, kill or reset ports **8000–8003** or **5180** (the owner's live demo). Tests use free ports
(`tests/conftest.py::_free_port`). Dev servers: fixture tracker form `18010`, W4a control plane `38080`, W4b UI dev server
`5181` (proxy to `38080`). Every manual run sets its own `PATCHWORK_WORKSPACE` (a temp dir), never `./workspace`.

Python: `/Users/hutch/.paradev/worktrees/hack-princeton/through-plan-create-68d423de/.venv/bin/python` (shared venv; new
worktrees have none: use this absolute path). New dependency: `selectolax==1.0.2` (import
`from selectolax.lexbor import LexborHTMLParser`; the default `selectolax.parser` backend raises ImportError in 1.0).
Model calls: OpenRouter free tier, **20/min and 1,000/day shared by everyone**. Default to the scripted agent and the
offline fake model. Per-workstream live-call budget: W3 ≤ 150, everyone else 0 (the integrator owns the rest).
Never print, log or commit `.env` or the key.

Demo targets (spike a, 2026-11-14): Form "Patchwork Test Form"
`https://docs.google.com/forms/d/e/1FAIpQLSeJSRbMx1pr9nMwj4n1E5vOUxmz2Zo19-lJXyz0-8HhmfeNoQ/viewform` (env `PATCHWORK_DEMO_FORM_URL`);
linked Sheet CSV `https://docs.google.com/spreadsheets/d/1sEDvJMO4sQN3RJxo1JKFqqfrDfQTjZrNL2Ztlz0j60M/gviz/tq?tqx=out:csv&sheet=Form%20Responses%201`
(env `PATCHWORK_DEMO_SHEET_CSV`; header `Timestamp, Email Address, Job Title, Company, Link, Date Applied`; rows visible < 1 s
after submit). Only the integrator's rehearsals and opt-in live checks submit to it; automated tests never do.

## V1. Page record (the page inbox's record, the integration's source record)

```json
{"id": "pg-000012", "seq": 12,
 "url": "https://jobs.lever.co/palantir/0ccbe620-...?lever-source=x",
 "canonical_url": "https://jobs.lever.co/palantir/0ccbe620-...",
 "site": "jobs.lever.co",
 "title": "<document.title as sent>",
 "html": "<document.documentElement.outerHTML as sent>",
 "text": "<runner.html.visible_text(html)>",
 "captured_at": "2026-11-14T14:02:11.123+00:00",
 "captured_date": "2026-11-14"}
```

- `id` = `pg-` + zero-padded `seq`; `seq` is a per-workspace counter starting at 1.
- `site` = `runner.html.site_of(url)`; `canonical_url` = `runner.html.canonical_url(url, html)`;
  `captured_date` = the control plane's **local** date at ingest (C7: "date applied" = the hotkey press).
- Pages are the rendered DOM from the extension, so readers are built and tested on rendered DOM (as in `fixtures/pages/`).

## V2. `runner/html.py` (W1; pure, deterministic, no network, no AI; everyone imports it)

```python
def normalize_ws(s: str) -> str                      # collapse all whitespace runs to one space, strip
def visible_text(html: str) -> str                   # body text without script/style/noscript/template/svg;
                                                     # one line per block element, each line normalize_ws'd, empty lines dropped
def site_of(url: str) -> str                         # lowercase host, "www." removed, no port
def canonical_url(url: str, html: str) -> str        # <link rel=canonical> if absolute http(s); else url without fragment
                                                     # and without utm_*, gh_src, lever-source, source, ref params
def css_all(html: str, selector: str, attr: str | None = None) -> list[str]   # normalize_ws'd text (or attribute) per match
def css_first(html: str, selector: str, attr: str | None = None) -> str | None
def jsonld_objects(html: str) -> list[dict]          # every object in every ld+json script; arrays and @graph flattened
def jsonld_get(html: str, path: str, type_: str = "JobPosting") -> Any | None  # dotted path with [n], on the first object of @type
def fingerprint_present(html: str, selector: str) -> bool
```
Unparseable HTML never raises: functions return empty results.

## V3. Extractors (spec `FieldExtract` additions)

```yaml
fields:
  title:   {css: ".posting-headline h2"}
  company: {jsonld: "hiringOrganization.name"}
  company: {css: "title", regex: "^Job Application for .* at (.+)$"}
  company: {css: "meta[property=\"og:site_name\"]", attr: content, regex: "^Careers at (.+)$"}
  company: {jsonld: "name", jsonld_type: Organization}
```

- Primary method: exactly one of `regex | json_path | between | css | jsonld`. `regex` may also appear **with** `css` or
  `jsonld` as a refine: applied to the normalised result, group 1 is the value.
- `css` reads `record[source.html_field]`; first match; text is `normalize_ws`'d; `attr` (any attribute name) reads that
  attribute instead. `jsonld` reads the same HTML via `jsonld_get` (`jsonld_type` default `JobPosting`).
- Nothing matched, empty value, or the refine didn't match → `Quarantine("PARSE_NO_MATCH", "<parser>.<field>: …")`.
- `attr` only with `css`; `jsonld_type` only with `jsonld`; `combine` only with a primary `regex`.
- Runtime helpers (in `runner/lib.py`, used by generated parsers **and** by the agents' `test_parser`, so tests and runs
  share one implementation): `extract_css(html, selector, attr, where) -> str`,
  `extract_jsonld(html, path, jsonld_type, where) -> Any`, `refine_regex(value, pattern: re.Pattern, where) -> str`.
  Each raises `Quarantine("PARSE_NO_MATCH", …)` on no match or an empty value.
- `Source.html_field: str | None = None` is new; specs using `css`/`jsonld`/`fingerprint` must set it (validator error otherwise).

## V4. Routing and layout provenance (spec `Parser` additions)

```yaml
parsers:
  - id: lever
    match: {site: jobs.lever.co}
    fingerprint: ".posting-headline"
    origin: {kind: learned, at: "2026-11-14T14:02:11+00:00", thread_id: learn-3f9a2c1b0d, approved_by: auto}
    fields: {title: {css: ".posting-headline h2"}, company: {jsonld: "hiringOrganization.name"}}
```

Route order per record: (1) the first parser whose non-empty `match` equals the record; (2) else the first parser whose
`fingerprint` is present in the record's HTML; (3) else the first parser with empty `match` and no `fingerprint` (v1
catch-all); (4) else `PARSE_NO_MATCH` "no parser for site=…; no fingerprint matched". A fingerprint picks the platform,
not the reader: if the routed parser fails, the record is `PARSE_NO_MATCH` (→ learn), never retried on another parser.
`Outcome` gains `routed_by: "match" | "fingerprint" | "default" | None` (stored in a new `records.routed_by` column).
`origin` (`kind: taught|learned|fixture`, `at`, `thread_id`, `approved_by: auto|human|null`) is metadata only, copied into
`manifest.json` as `parsers: [{id, match, fingerprint, origin}]`. A spec with no fingerprints routes exactly as v1.

## V5. Form sink (`sink.kind: form`)

```yaml
sink:
  kind: form
  url: https://docs.google.com/forms/d/e/<id>/viewform
  fields:
    Email:        {const: demo@example.com}     # the demo Form collects a required email; taught as a constant
    Job Title:    {from: title}
    Company:      {from: company}
    Link:         {from: record.canonical_url}
    Date Applied: {from: record.captured_date}
  submit: Submit
  verify: {confirmation_text: "Your response has been recorded"}
```

- `Sink` becomes a union discriminated by `kind`: `ApiSink` (v1, unchanged) | `FormSink`. Keys of `fields` are form
  labels; values use the same `ValueSource` vocabulary and the same reference checks as `body`.
- Runtime (W1, `runner/forms.py`, Playwright headless Chromium, one browser per run, a fresh page per record):
  open `url` → snapshot (V6) → resolve each label to **exactly one** control: first by accessible name, else the single
  control inside the `role=listitem` whose `role=heading` text equals the label (both `normalize_ws`'d, case-insensitive,
  required markers `*` / `(required)` / `[aria-label="Required question"]` ignored; spike a: Google's Email input is
  labelled "Your email", not by its heading) → fill (text-like: fill; radio/select/checkbox: the option equal
  to the value) → click the button (`button` or `role=button`) whose accessible name **equals** `submit` (Google also shows "Clear form")
  → wait up to 10 s for `confirmation_text`.
  0 or >1 controls for a label, a missing option, or a missing submit button → `SINK_CONTRACT`; no confirmation →
  `VERIFY_MISMATCH`; page didn't load in 15 s → `UNKNOWN`.
- **Dry run** (`--dry-run`): open, snapshot, resolve, fill, **never click submit**; the outcome is `success` with detail
  `"dry run: filled, not submitted"`. Form sinks have no staging URL; "dry run never submits" is the guarantee and is
  enforced in `runner/forms.py`, not by callers.
- `runner/forms.py` API: `snapshot_form(page) -> list[dict]` (`{label, input, required, options}`),
  `form_contract(snapshot, *, title, url) -> dict` (V6), `fill_and_submit(page, values: dict[str, str], *, submit, confirmation_text, dry_run) -> None`,
  `fetch_form_contract(url: str) -> dict` (sync; launches its own browser; for the compiler, control plane and agents).
- Values sent to a form are strings (`str()` of the mapped value).
- *Added by W1 (additive):* `fill_and_submit(..., confirm_timeout_ms=10_000)`; helpers `open_form(page, url, *, timeout_ms=15_000)`
  (raises `UNKNOWN` if the form doesn't load) and `resolve_control(page, label) -> (kind, locator)` (raises `SINK_CONTRACT`).
  Every label **and** the submit button are resolved before anything is typed, so a failing record leaves nothing half
  filled. A name match that disagrees with a heading match (a control *named* "Company" plus a different question
  *titled* "Company") is ambiguous → `SINK_CONTRACT`. Each fill/click has a 5 s timeout (`SINK_CONTRACT` if a control
  can't be filled). An empty required answer shows up as `VERIFY_MISMATCH` with the page's alerts in the detail.
  `FormSession(dry_run)` (one browser per run, a fresh context per record) and `FormResult(status_code, text, detail)`
  are what the runner uses; a dry-run record's outcome `detail` is `"dry run: filled, not submitted"`.
- *Runtime surface (W1):* `runner.http.Http` (and `runner.testing.FakeHttp`) gain `form_submit(url, values, *, submit,
  confirmation_text)` and `form_contract(url)`; generated form adapters call those. Generated `integration.py` exposes
  `PIPELINE_API = 2`, `route(record) -> (parser_id, fn, routed_by)`, `fetch_contract(http)` and `SINK_KIND`;
  `extract(record) -> (parser_id, fields)` is unchanged.

## V6. Form contract document (an OpenAPI-shaped synthesised doc, so v1's contract machinery keeps working)

```json
{"openapi": "3.0.3",
 "info": {"title": "Job tracker", "x-patchwork-kind": "form", "x-form-url": "https://docs.google.com/forms/d/e/<id>/viewform"},
 "paths": {"/form": {"post": {"requestBody": {"content": {"application/json": {"schema": {
   "type": "object", "additionalProperties": false,
   "required": ["Email"],
   "properties": {"Email": {"type": "string", "x-input": "email"},
                  "Job Title": {"type": "string", "x-input": "text"},
                  "Seniority": {"type": "string", "x-input": "radio", "enum": ["Junior", "Senior"]}}}}}}}}}}
```

- `x-input` ∈ `text|textarea|email|url|date|number|radio|checkbox|select`; `type` is always `string`. No volatile data
  (no tokens, ids, timestamps; on Google: not the generated `iN` ids, `data-params`, `fbzx`, `jsname` or classes): loading an
  unchanged form twice gives the same `runner.contract.openapi_hash` (spike a: stable across loads).
- *Added by W1:* `info.title` is the page's `document.title`. Radio/select options become `enum`; checkbox options become
  `x-options` (several may be ticked, so not an enum). If two questions share a label the property gets `"x-count": N`
  (filling that label fails closed). Google's email-collection question (and the fixture's) snapshots as `x-input: email`.
- Compiled packages store it at `contract/openapi.json` as in v1; `manifest.json` has `sink_call: {method: POST, path: /form}`
  and `contract_kind: "form"` (`"openapi"` for API sinks).
- **Preflight** (runner, before any write): fetch the live contract in the run's browser, compare hashes; if changed, run
  `runner.contract.validate_body` on the golden bodies; errors → block, `CONTRACT_CHANGED` incident with the same fields
  as v1 (`contract_errors`, `sink_call`, `old_openapi`, `new_openapi`). "Company" → "Employer" yields
  `body.Company: not accepted by destination (no such property)` and, if `Employer` is required,
  `body.Employer: required by destination but missing from body`.
- `control.ops.fetch_contract(spec) -> dict` (W1): API sinks → `GET {base}/openapi.json` (today's `fetch_sink_openapi`,
  kept as an alias); form sinks → `runner.forms.fetch_form_contract(url)`. `compile_and_test` and the agents' tools use it.

## V7. `jobs_to_tracker` source and the control URL

```yaml
source:
  kind: api
  base_url: patchwork://control
  list: {method: GET, path: /api/pages, params: {since: "{cursor}"}}
  record_id: id
  text_field: text
  html_field: html
  cursor_field: seq
dedupe: {key: [record.canonical_url]}          # C5
validate: [{field: title, rule: required}, {field: company, rule: required}]
```

`runner/http.py` resolves `patchwork://control` to `$PATCHWORK_CONTROL_URL` (the control plane sets it for every runner it
launches; the runner fails with a clear message if it's missing). The global `PATCHWORK_SOURCE_BASE_URL` override does
**not** apply to `patchwork://control` and `PATCHWORK_SINK_BASE_URL` does not apply to form sinks, so v1 and v2 can run
from one control plane. Parsers extract only page fields (`title`, `company`); link and date come from `record.*` and are
exempt from the learn lane's evidence check (C7). `fixtures/jobs/spec.yaml` + `examples.yaml` (W1) is a **test fixture**
built against `fixtures/pages/` and the fixture tracker form; per C8 it is never what runs on stage.
*Added by W1:* a missing `PATCHWORK_CONTROL_URL` is recorded as a run with `status: failed` and a `message` naming the
variable (exit code 1), not a traceback. `fixtures/jobs/pages.py` has `page_record(seq, url, html, *, captured_at, ...)`
(a V1 record builder) and `fixture_records()` for tests; `python -m fixtures.jobs.pages` regenerates `examples.yaml`
(slimmed HTML: non-JSON-LD scripts, styles, SVGs and comments removed; readers and visible text unchanged).

## V8. Fixture tracker form (`demo_apps/tracker_form/`, W1)

`python -m demo_apps.tracker_form --port 18010`.
Default questions copy the demo Google Form (spike a, `fixtures/google_form/viewform.html`): `Email` (required, input
`aria-label="Your email"`, not tied to its heading), then `Job Title`, `Company`, `Link`, `Date Applied` (short answer, not
required). Markup mimics Google Forms: each question is `div[role=listitem]` with a `div[role=heading][aria-level=3]`
title (required ones end with `<span aria-label="Required question"> *</span>`) and an `input type=text` whose
`aria-labelledby` points at the heading; `div[role=button][aria-label=Submit]` and `div[role=button][aria-label="Clear form"]`;
submitting leads to `/formResponse` saying `Your response has been recorded.` Empty required → `role=alert`
"This is a required question", no row. `create_app(questions=[{label, required}] | None)`.
`GET /` form · `POST /submit` · `GET /admin/rows` → `[{<label>: value, "_ts": iso}]` · `POST /admin/rename {from, to}` ·
`POST /admin/reset` (rows cleared, labels back to default).

## V9. Control-plane API additions (W4a; consumed by W2's extension, W4b's UI and W5's smoke)

```
POST /api/pages {url, title, html}       header X-Patchwork-Token
     -> 201 {id, seq, site, canonical_url, integration: str|null, run_id: int|null, queued: bool}
     401 bad or missing token · 413 html > 5 MB · 422 bad body. CORS allows chrome-extension:// origins.
GET  /api/pages?since=<seq>&limit=<n>    -> [page record] ascending by seq, `since` exclusive ("" = 0). No token (localhost).
GET  /api/pages/{id}                     -> {page: <record without html>, row: <TrackerRow>}
GET  /api/extension                      -> {token, control_url}     (setup values the UI shows)
GET  /api/tracker?integration=jobs_to_tracker&limit=50
     -> {rows: [TrackerRow] newest first, totals: {pages, ai_calls, code, learned_now, held, duplicate}}
GET  /api/layouts?integration=jobs_to_tracker -> [Layout]
POST /api/layouts/{parser_id}/revert {integration}
     -> {version, deployed: true}   compiles active spec minus that parser, tests, deploys (no AI). 409 if tests fail or last parser.
GET/PUT /api/settings                    adds "learn_policy": "auto"|"approve" (default "auto", D2)
POST /api/integrations/build {recording_id, name?}   name defaults to "jobs_to_tracker" for page→form recordings
POST /api/demo/form/rename {from, to}    proxies the fixture form ($PATCHWORK_TRACKER_FORM_URL); offline rehearsal only
POST /api/demo/v2/save-baseline          remember jobs_to_tracker's versions + active pointer as the rehearsal baseline
POST /api/demo/v2/reset                  restore that baseline; clear pages, jobs_to_tracker runs/records/processed/cursor/incidents
                                         and learn threads. Never touches po_to_stockroom. (v1's /api/demo/reset is unchanged.)
GET  /api/bench/v2                       bench/v2/results.json (W5); 404 until it exists
```

TrackerRow:
```json
{"page_id": "pg-000012", "seq": 12, "captured_at": "…", "site": "jobs.lever.co", "url": "…",
 "title": "American Tech Fellowship", "company": "Palantir Technologies",
 "status": "pending|running|success|duplicate|held|learning|failed",
 "label": "code|learned_now|held|duplicate|null",
 "reason": "PARSE_NO_MATCH|…|null", "detail": null,
 "parser": "lever", "version": 3, "routed_by": "match",
 "parser_origin": {"kind": "learned", "at": "…", "thread_id": "learn-…", "approved_by": "auto"},
 "ai_calls": 0, "run_id": 7, "learn_thread_id": null, "incident_id": null}
```
`label`: success whose parser's `origin.thread_id` is this page's learn thread → `learned_now`; other success → `code`;
quarantined or blocked → `held`; duplicate → `duplicate`. `ai_calls` = model calls of the learn thread started for this
page (0 if none; the scripted agent reports 0 and the UI says "scripted"). `title`/`company` are the extracted values when
available, else the page title and null.

Layout: `{integration, parser_id, site, fingerprint, fields, origin, introduced_in: <version>, active: bool}` from the
manifests of the integration's versions.

**Run on arrival.** After storing a page: for each integration whose active spec has `source.base_url: patchwork://control`,
start a run; if one is in progress, queue exactly one follow-up run. When such a run finishes: for each record
`PARSE_NO_MATCH`, if AI is on and no learn thread is running for that `site`, `agent.start_learn(page_id, integration)`;
if AI is off, the row stays `held`. When a learn or repair thread emits `deployed` for such an integration: replay
quarantined records, then run once more (pages blocked by preflight were never fetched past the cursor). Repairs of
`CONTRACT_CHANGED` still start from a person's click (as in v1).

## V10. Agents (W3): LEARN mode and the TEACH/REPAIR additions

```python
class AgentService(Protocol):              # additions; v1 methods unchanged
    def start_learn(self, page_id: str, integration: str = "jobs_to_tracker") -> str: ...   # thread id "learn-<hex>"
    def start_build(self, recording_id: str, name: str | None = None) -> str: ...
```
`Mode` adds `"learn"`. New event types (added to `EVENT_TYPES`; UIs must ignore unknown types):
```
"evidence": ["page_id", "parser_id", "fields", "ok"]   # fields: [{name, value, ok, where: "text"|"html"|"jsonld"|null, extractor}]
"learned":  ["integration", "parser_id", "site", "version", "deployed", "approval", "reason"]   # approval: auto|human|pending
```
LEARN `done.result`: `{model_calls, seconds, page_id, parser_id, site, version, deployed, approval}`.

- **Evidence check (code, `agents/deep_tools.py`):** every page-extracted value must occur, after `normalize_ws` on both
  sides, in the page's visible text, in an attribute value in the HTML, or in a JSON-LD string, and must pass the spec's
  validation rules. `record.*` values are exempt.
- **Ambiguity check (code):** a field is ambiguous if its css selector matches elements with different normalised values,
  or if the learner proposed two passing extractors that disagree. Ambiguous → `ask_user` with the distinct values as options.
- **Corroboration check (code):** a page value is *corroborated* if it also occurs (normalised, case-insensitive) in the
  record's `title` (document.title), an `og:*`/`twitter:*`/`<title>` meta value, or a JSON-LD string. Spike b: title and
  company are corroborated on all four platforms; page chrome (cookie banners, "Apply") is not.
- **D2 auto-use, enforced in a tool:** the learned version is deployed without asking iff `learn_policy == "auto"`, the
  exact version passed its full suite, every evidence check passed, every value is corroborated, nothing was ambiguous in this thread, and the JSON Patch
  from the active spec only adds one parser (`add` ops under `/extract/parsers`). Otherwise `request_deploy` (a person).
  Either way the parser's `origin` records it (`approved_by: auto|human`).
- Learn budget: target ≤ 8 model calls on the deep agent; the learner sees a compact page outline from a tool, never the
  whole HTML. `ScriptedAgentService.start_learn` emits the same event sequence with no model.
- REPAIR on a form `CONTRACT_CHANGED`: same `start_repair(incident_id)`; the fix is a JSON Patch `move` of the renamed
  `sink.fields` key plus migrated golden examples; `request_deploy` always (a person approves).
- TEACH from a page→form recording (`start_build(recording_id, name)`) uses V11 candidates and produces a spec shaped as V7
  with one parser (`origin.kind: taught`).

## V11. Capture (W2): page snapshots, element paths, page→form candidates, extension

Recorder: every `copy` event adds `el.path` (a stable CSS path: `#id` if unique, else tag + stable classes with
`:nth-of-type` as needed, verified unique with `querySelectorAll`) and `snapshot: "snapshots/<n>.html"` (outerHTML at copy
time, saved in the recording dir). Allowlist entries may be bare hosts (`docs.google.com`, `jobs.lever.co`).

`candidates.json` for a page→form recording (no mutating JSON request is seen):
```json
{"recording_id": "…", "intent": "…",
 "source": {"kind": "page", "site": "jobs.lever.co", "url": "…", "snapshot": "snapshots/1.html"},
 "sink": {"kind": "form", "url": "https://docs.google.com/forms/d/e/<id>/viewform",
          "labels": ["Email", "Job Title", "Company", "Link", "Date Applied"], "submit": "Submit",
          "confirmation_text": "Your response has been recorded"},
 "mappings": [
   {"dest_field": "Job Title", "value": "American Tech Fellowship", "kind": "css", "css": ".posting-headline h2",
    "attr": null, "regex": null, "copied": true, "alternatives": [{"jsonld": "title"}]},
   {"dest_field": "Link", "value": "https://jobs.lever.co/…", "kind": "record", "from": "record.canonical_url"},
   {"dest_field": "Date Applied", "value": "2026-11-14", "kind": "record", "from": "record.captured_date"},
   {"dest_field": "Email", "value": "demo@example.com", "kind": "const"}],
 "unexplained": []}
```
Fixture recording for everyone: `fixtures/recording_jobs/` (`recording.json`, `candidates.json`, `snapshots/`), recording
id `fixture-jobs` in the control plane, recorded against `fixtures/pages/lever-1.html` and the fixture tracker form.

Extension (`extension/`, Chrome MV3): command `send-page` (default `Alt+Shift+P`); takes the active tab's URL,
`document.title` and `document.documentElement.outerHTML` via `chrome.scripting`, `POST {controlUrl}/api/pages` with
`X-Patchwork-Token`; options in `chrome.storage.local`: `controlUrl` (default `http://127.0.0.1:8000`), `token`. Badge:
`…` sending, `✓` code, `AI` learned now, `!` held or failed (polls `GET /api/pages/{id}` up to 30 s).

## V12. Proof (W5)

`fixtures/postings/answer_key.json`: `[{file, platform, site, url, fetched_at, title, company}]`, pages as
`fixtures/postings/<platform>/<n>.html.gz` (rendered DOM). `bench/v2/results.json`:
`{postings: [{n, file, site, label, ai_calls, seconds, correct}], series: {patchwork_cumulative_ai_calls: [...],
browser_agent_cumulative_ai_calls: [...]}, numbers: [{name, value, unit, kind, source}]}` with `kind` ∈
`measured | measured (earlier run) | estimate | extrapolated | not measured` as in v1.
