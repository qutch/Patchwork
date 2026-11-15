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

**Platform wildcards** (added after the stage rehearsal). Multi-tenant job platforms give each company its own host
(`visa.wd5.myworkdayjobs.com`, `tmobile.wd1.myworkdayjobs.com`) for one layout. A `match` value `*.<domain>` (at least two
labels, e.g. `site: "*.myworkdayjobs.com"`) covers every host ending in `.<domain>` (`runner.lib.match_rank`: 2 exact,
1 wildcard, 0 none). Step (1) above becomes: the first parser whose `match` equals the record exactly, else the first
whose wildcard covers it, so a company's own parser beats its platform's whatever their order. The platforms are
`runner.html.TENANT_PLATFORMS` (`platform_of(site)` → `*.<domain>` or None). LEARN decides the scope in code
(`save_parser`): a parser for a tenant's page gets the platform match if the platform has no parser yet and it passes
the evidence checks on at least half of the other companies' stored pages there (newest page per host, at most 5
hosts); otherwise it is saved for that host. A company the platform parser misses is held (`PARSE_NO_MATCH`, never a
wrong row) and learns its own exact-host parser. An explicit wildcard `match` that fails that check is `REFUSED`. One learn runs per platform at a
time (`control/page_hooks.py`); held pages of other tenants are replayed after it deploys. Separately, a regex whose
group 1 is fixed text (`About Us (Visa) is`) fails evidence in every LEARN check: it writes a constant, not a reading.

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
POST /api/integrations/build {recording_id, name?, form_url?}   name defaults to "jobs_to_tracker" for page→form recordings;
     form_url (page→form only, http(s)) teaches against that form instead of the recorded one: the control plane writes the
     recording's candidates with sink.url replaced (sink.retargeted_from keeps the old one) to $WS/recordings/<id>/candidates.json,
     which the agents read first. Used to point fixture-jobs at the demo Google Form (I-2) or at a fixture form on a free port.
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

**Details fixed while building W4a (additive; nothing above was removed or renamed):**
- Token: `$PATCHWORK_WORKSPACE/extension_token` (created on first use, mode 0600; `PATCHWORK_EXTENSION_TOKEN` overrides).
  Wrong or missing → 401. Body > 15 MB is refused before parsing, then `html` > 5 MB (UTF-8 bytes) → 413. 422 messages
  never echo the body.
- `POST /api/pages` may add `run_error` (the page is stored, the runner failed before `started`). `integration` is the
  first page integration (`jobs_to_tracker` first); every page integration gets a run. `run_id` is null whenever `queued`.
- CORS for `chrome-extension://<id>` origins only on `POST /api/pages` and `GET /api/pages/{id}` (not the html-bearing list).
- `PATCHWORK_CONTROL_URL`: an explicit env value wins; otherwise the control plane uses the address it is serving on
  (learned from the first request). `control.ops.runner_env()` adds it, so `run_sandboxed`/`replay_quarantined` get it too.
- `GET /api/pages/{id}` takes optional `?integration=`. `GET /api/pages?since=` also accepts `None`/`null` as 0; a
  non-integer → 422; `limit` 1–1000.
- Page ids are never reused in a workspace (the seq counter survives `/api/demo/v2/reset`).
- TrackerRow: a page with no record yet whose latest run (started after the page) was `blocked` by preflight is
  `status: held, label: held, reason: "CONTRACT_CHANGED"` with that run's `incident_id`; after a `failed`/`tripped` run it
  is `status: failed`. A quarantined page whose learn thread is running/waiting is `status: learning` (label null); one
  whose learn deployed but whose replay hasn't finished is `status: running`. `totals` count every page in the inbox
  (not just `limit`). Which page started which learn thread is kept in `pages.db` (`learns`), so labels survive a restart.
- Learn trigger: a page is learned from at most once (a later replay of it never starts a second thread); one active
  (running/waiting) learn per `site`. Agents without `start_learn` or with AI off → rows stay `held`. Only `deployed`
  events from `learn-*`/`repair-*` threads trigger the replay + run, after that thread has finished.
- Layouts: active parsers first (active spec order), then removed ones (newest first). Revert also answers
  `name, previous_version, removed_parser, dropped_examples` (golden examples the removed parser handled are dropped);
  404 for an unknown integration or parser.
- Settings: `PUT /api/settings` takes `ai_enabled` and/or `learn_policy` (both optional, at least one; other values 422).
  Agents read it with `control.api.settings.Settings().learn_policy` (no FastAPI import).
- Build: with no `name`, page→form recordings (`fixture-jobs`, or candidates with `sink.kind: form` / `source.kind: page`)
  get `jobs_to_tracker`; others call `start_build(recording_id)` as in v1. Agents whose `start_build` takes only the
  recording id still work.
- Recordings: `fixture-jobs` (`fixtures/recording_jobs/recording.json`) is listed after `fixture`, with `fixture: true`,
  only once that file exists; its candidates come from `fixtures/recording_jobs/candidates.json` until re-matched.
- `POST /api/demo/v2/save-baseline [{integration}]` → `{integration, active_version, versions, has_work, saved_at}` (404
  unknown integration, 409 no active version); it also snapshots `integrations/<name>/work/` (the agent's files).
  `POST /api/demo/v2/reset` → `{ok, integration, active_version, removed: {runs, records, processed, incidents, versions,
  pages}, stopped_runs, cancelled_threads, form?}`; 409 before a baseline is saved. It deletes versions newer than the
  baseline (rows and package dirs), restores the work dir, and resets the fixture form (`/admin/reset`) only when
  `PATCHWORK_TRACKER_FORM_URL` is set. Agents with a `cancel(thread_id)` method are asked to cancel; otherwise the
  ThreadedAgentService threads are cancelled the way `reset()` does, but only the integration's learn/repair threads.
- `POST /api/demo/form/rename` passes the form's status/JSON through (502 unreachable, 422 without `from`/`to`).
  `GET /api/bench/v2`: `PATCHWORK_BENCH_V2_RESULTS` overrides the path.

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

**Details fixed while building W3 (additive):**
- Thread status rows add `integration` (BUILD: the requested name or null; LEARN: the integration).
- LEARN tools (`agents/deep_tools.py`, plain functions in `tool_functions(ctx)`, no langchain import, so the scripted
  agent calls the same code): `get_page(page_id?)` (outline + the fields every parser must extract), `test_page_parser(parser)`
  (run + evidence + corroboration + ambiguity; emits `evidence`), `save_parser(parser, reason)`. `save_parser` only works
  on the thread's own page, defaults `match` to `{site: <page site>}`, refuses a field set different from the active
  parsers', an existing parser id, a `match`/`fingerprint` that doesn't fit the page, and any value without evidence (no
  compile). It seeds a golden example from the page, compiles, and applies D2 itself.
- Ambiguity is asked by `save_parser` (code), once per field: `ask_user` "Which is the <field> on this page (<site>)?"
  with the distinct values + "None of these". An answer that differs from the parser's value → `REFUSED` (fix the
  selector). A field asked about still counts as ambiguous for D2 (a person approves).
- Corroboration: matches are whole words; a source longer than 200 chars (e.g. `og:description`, JSON-LD
  `description`) only counts on an exact match, so the posting body can't corroborate itself.
- Evidence `where: "html"` covers attribute values and the `<title>` text. Content hidden from visitors (`noscript`,
  `script`, `template`) is not visible text.
- `learned` events: auto path → one event (`approval: auto`, `deployed: true`) right after the deploy and before
  `deployed`; ask path → `approval: pending` before `request_deploy`, then `approval: human` with the outcome.
  The parser's `origin.approved_by` is `auto` or `human` accordingly (a rejected version keeps `human`, never deployed).
- `learn_policy` is read from `$PATCHWORK_WORKSPACE/settings.json` (`"auto"` default); an unknown value counts as `"approve"`.
- For page integrations (`source.base_url: patchwork://control`) the agent never replays after `deployed` (LEARN or
  REPAIR): the control plane does (V9). LEARN's filesystem allows writing `/spec.yaml` only.
- TEACH page→form (`draft_spec` on V11 candidates): parser id from the site (`jobs.lever.co` → `lever`,
  `careers.airbnb.com` → `airbnb`), `match: {site}`, no fingerprint, `origin: {kind: taught, approved_by: null}`;
  `extract.route_by: site`; a mapping without a selector uses its first css/jsonld `alternatives` entry; field names
  `Job Title` → `title`, `Company` → `company` (else a slug of the label); `validate: required` per page field; one
  golden example built from the snapshot (record.* values from the record, page values as recorded). `start_build`
  without a name builds `jobs_to_tracker` when the candidates are page→form. Until W2's `fixtures/recording_jobs/`
  lands, `fixture-jobs` falls back to `fixtures/agent_candidates_jobs.json` (V11 shape, snapshot `fixtures/pages/lever-1.html`).
- REPAIR on a form: `diff_openapi` pairs renames (name similarity, feeding field, then a unique same-type pair), returns
  `patch_hint` (the `move` ops on `/sink/fields`) and `suggested_questions` only for questions it couldn't pair
  ("The form question 'X' is gone. Which new question replaces it?", options = unpaired new labels + "None: stop sending it").
  `propose_spec_patch` refuses ops on `/sink/url`, `/sink/kind`, `/source/kind` (as for base URLs). Golden bodies of
  form specs are strings. Dry runs of form versions need no staging URL.

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

**Additions made while building W2 (all additive):**
- Recorder events: controls inside a Google-Forms-like `role=listitem` add `el.question` (the heading text, required
  marker removed; absent elsewhere, so v1 shapes are unchanged). A click on a `role=button` element of such a form adds
  `form: {questions: [{label, required, input}], title}` (`input` uses V6's `x-input` names). The next page on the same
  origin within 60 s of that click emits `{type: "confirmation", lines: [visible text lines, ≤ 20], title}`.
  Snapshots drop the `value` of password / cc-* / one-time-code inputs; an unchanged DOM is stored once.
- `matcher.match(recording, base_dir=None)`: `base_dir` (the recording dir) lets the matcher read snapshots; the
  recorder and `python -m matcher <path>` pass it. Without it, css mappings fall back to the recorded `el.path`.
  The matcher may import `runner.html` (pure, no network).
- Page→form candidates also carry `source.canonical_url`, `source.title` (document title at the source),
  `sink.title` and `sink.questions` (`[{label, required, input}]`). Mapping kinds: `css` (`css`, `attr`, `regex`
  refine, `copied`), `jsonld` (`jsonld`, optional `jsonld_type` when not `JobPosting`; a value typed by hand that the
  page shows), `record` (`from`), `const` (typed, found nowhere). A pasted value found nowhere is `unexplained`.
  `alternatives` hold other readers that give exactly the same value: `{"jsonld": path[, "jsonld_type"]}` or
  `{"css": "meta[property=\"og:title\"]", "attr": "content"}`.
- In `fixtures/recording_jobs/` the form is reached as `http://127.0.0.1:18010/` (V0's fixture-form dev port, routed by
  Playwright in the script, never bound), so `sink.url` is stable; point it at your own form in tests.
  The copied company comes from Lever's footer link (`css` + `regex: "^(.+?) Home Page$"`, alternative
  `{"jsonld": "hiringOrganization.name"}`). Regenerate with `scripts/make_recording_jobs.py`.
- Extension: badge `dup` for `label: duplicate` (V9), and `!` also for wrong token / unreachable control plane / non-web
  tab (the button's tooltip says which). Host permissions are `http://127.0.0.1/*` and `http://localhost/*` (any port).
  The options page also has "Get token from Patchwork", which reads `GET {controlUrl}/api/extension`. The service
  worker is a classic script, so `sendActiveTab(tab?)` is a global that tests call through
  `context.service_workers[0].evaluate`. It resolves to `{ok, id, state: "code"|"learned_now"|"held"|"duplicate"|null, row}`
  or `{ok: false, error}`.

Extension (`extension/`, Chrome MV3): command `send-page` (default `Alt+Shift+P`); takes the active tab's URL,
`document.title` and `document.documentElement.outerHTML` via `chrome.scripting`, `POST {controlUrl}/api/pages` with
`X-Patchwork-Token`; options in `chrome.storage.local`: `controlUrl` (default `auto`), `token`. Badge:
`…` sending, `✓` code, `AI` learned now, `!` held or failed (polls `GET /api/pages/{id}` up to 30 s).
- Additive (one-command launcher, 2026-11-14): with `controlUrl` `auto` (or absent) the extension finds Patchwork by
  itself (`extension/discover.js`): `GET http://127.0.0.1:{38091..38110}/api/extension`, all at once with a short
  timeout, and the first port in that order that answers with a `token` wins. The address is cached as `autoUrl`
  and the token as `token`. After a network failure or a 401 it looks again once and retries. A `controlUrl` set by
  hand (options, "Advanced") always wins and is never replaced. `discoverPorts` (a list of ports) replaces the
  range; only tests set it.

## V12. Proof (W5)

`fixtures/postings/answer_key.json`: `[{file, platform, site, url, fetched_at, title, company}]`, pages as
`fixtures/postings/<platform>/<n>.html.gz` (rendered DOM). Additive (W5): each entry also has `board`, `requested_url`,
`document_title` (send it as the page `title`, as the extension would), `accept: {field: [other accepted spellings]}`
(empty unless stated), `method: {title, company}` (how each value was decided), `checks: {name: bool}` (which
independent sources agreed), `notes` (resolved disagreements and `trap:` lines: page chrome or escaped JSON-LD a
learned reader could pick) and `checked_by`. Provenance: `plan.json` (listing calls) and `fetch_log.json` (renders).
Platforms: lever, greenhouse (job-boards.greenhouse.io), greenhouse_branded (careers.airbnb.com), ashby,
smartrecruiters; 12 each. `bench/v2/results.json`:
`{postings: [{n, file, site, label, ai_calls, seconds, correct}], series: {patchwork_cumulative_ai_calls: [...],
browser_agent_cumulative_ai_calls: [...]}, numbers: [{name, value, unit, kind, source}]}` with `kind` ∈
`measured | measured (earlier run) | estimate | extrapolated | not measured` as in v1.

Additive (W5) in `results.json`: each posting also has `platform, status, reason, parser, version, routed_by,
learn_thread_id, thread_model_calls, ai_calls_kind, asked_person, outcome (right_write|wrong_write|held|dropped), got,
want, error`; `correct` is `true` (right row), `false` (wrong row or nothing written and not held) or `null` (held for
a person). `series_meta: {<series key>: {kind, source, note|assumption}}` labels each series (the browser-agent one is
an `estimate` and must carry its `assumption`); `numbers[]` may have a `note`; plus `schema_version, generated_at,
command, environment, scenario, questions, summary`. `bench.v2.scoring.check_results` rejects a file that breaks this.
UIs read `series` + `series_meta` for the chart and `numbers` for cards.

What the harness and `scripts/smoke_v2.py` rely on (W5 reads, others own):
- `POST /api/integrations/build {recording_id: "fixture-jobs", name: "jobs_to_tracker"}` → a thread answered through
  `/api/agent/{id}/resume` as in v1; `PUT /api/settings {ai_enabled, learn_policy}`.
- `TrackerRow.ai_calls` is the only source of a learn's model calls over HTTP (agent status has no `result`), so W4a
  must fill it from the learn thread's `done.result.model_calls` (V10).
- After a repair deploys, the held page's row goes `held` → `success` once the replay/run has finished; the smoke waits
  for `success` on that page id.
- `GET /api/incidents/{id}` → `detail.contract_errors` (v1 shape) for the form `CONTRACT_CHANGED`.
- The fixture recording's form URL is whatever W2-3 recorded against (`:18010`); the in-process bench/smoke run the
  fixture form on a free port and, after TEACH, **retarget** the taught spec's `sink.url` only (compile, test, deploy
  through `control.ops`, recorded in `results.json` `scenario.retarget`). `POST /api/demo/v2/reset` does not reset the
  fixture form; the smoke calls the form's `/admin/reset` itself.

---

# Part 3: Destinations and generalization (v3, 2026-11-14)

Goal: the demo shows four destinations (Google Form, any labelled HTML form, Airtable, Postgres/SQLite) and integrations
that are not about jobs. This part covers the first two steps (credentials, Airtable) and the generalization; generic
HTML forms and SQL come later and will follow V14's shape (a label-keyed sink with an OpenAPI-shaped contract).

Non-negotiable, as before: the runner stays AI-free (stripped env, no model keys, refuses AI modules); never silently
write a wrong row; every AI-produced value checked by code; v1 (`po_to_stockroom`) and v2 (`jobs_to_tracker` on the
Google Form) keep working unchanged and every existing test stays green; ports 8000-8003/5180 and 38091 (the owner's
stage) are never bound, killed or reset; offline fakes in tests, no live model or Airtable calls in the suite.

## Ownership (Part 3)

| Stream | Owns | Must not touch |
|---|---|---|
| **A1 credentials** | `control/ops.py` (runner_env), `runner/http.py` (auth on API sinks/sources), `spec/models.py` (`auth` on `ApiSink`/`Source`), `control/api` `GET /api/credentials`, redaction in agent tools and run logs | Airtable files, web/ |
| **A2 Airtable** | `runner/airtable.py`, `compiler/templates/adapters_airtable.py.j2`, `spec/models.py` (`AirtableSink`), `compiler/compile.py` (sink dispatch), `demo_apps/fake_airtable/`, label-keyed sink support in `agents/` (TEACH/REPAIR), `POST /api/integrations/build` destination option, smoke/bench flags | web/, page routing |
| **B1 generalize (backend)** | prompts, `agents/learn.py` jsonld types, page→integration routing (`control/pages.py`, `control/api/pages.py`, `control/autorun.py`, extension), tracker API fields, demo endpoints `?integration=`, new validation rules (compiler + learn) | Airtable, credentials, web/ |
| **B2 generalize (UI)** | `web/` | everything else |

`runner/credentials.py` and `spec.models.Auth` are written by the lead (below) and are the shared base; extend, don't fork.

## V13. Credentials

- A spec names a credential, never a value: `auth: {scheme: bearer, credential: AIRTABLE_TOKEN}` or
  `{scheme: header, header: X-Api-Key, credential: STOCK_API_KEY}` (`spec.models.Auth`). Names are UPPER_SNAKE_CASE and
  may not match `runner.credentials.MODEL_KEY` (Anthropic/OpenRouter/OpenAI/LangChain/... keys): validation error.
- Values live in `.env` (gitignored), loaded by the control plane. `control.ops.runner_env(spec=None)` (A1) adds
  `PATCHWORK_CRED__<NAME>=<value>` **only for the names the run's spec references**, after the existing secret strip;
  a missing value → the run fails to start with `credential <NAME> is not set: add <NAME>=... to .env` (no value shown).
  Generated tests (`run_tests`) never get credentials: they use `runner.testing.FakeHttp`.
- Runner side: `runner.credentials.get(name)` / `headers(auth)`; `runner/http.py` sends `headers(spec.sink.auth)` on
  every sink request and `headers(spec.source.auth)` on source requests (config.yaml carries `auth` blocks, no values).
  401/403 from a sink → `Quarantine("AUTH", "<sink> rejected the credential <NAME> (HTTP 401)")`.
- Control-plane side (contract fetch for preflight/agents' `read_openapi`): the same `get()` reads the plain env var.
- Nothing that leaves a process shows a value: run logs, quarantine details, incident payloads, agent tool results and
  API error responses pass through `runner.credentials.redact(text, names=<credentials the spec references>)`.
  Contract documents never include auth headers. A test asserts a sentinel token never appears in any of them.
- `GET /api/credentials` → `[{name, set: bool, used_by: [integration, ...]}]` (never values).

## V14. Airtable sink (`sink.kind: airtable`)

```yaml
sink:
  kind: airtable
  base_id: appXXXXXXXXXXXXXX          # from the base URL airtable.com/appXXXX/tblYYYY/...
  table: Applications                 # table name or id (tbl...)
  auth: {scheme: bearer, credential: AIRTABLE_TOKEN}
  fields:                             # keyed by Airtable field NAME, like form labels; same value vocabulary as body
    Job Title: {from: title}
    Company: {from: company}
    Link: {from: record.canonical_url}
    Date Applied: {from: record.captured_date}
  typecast: false
  verify: {read_back: true}
```

- API base `https://api.airtable.com/v0`, overridden by `PATCHWORK_AIRTABLE_API_URL` (the fake in tests/smoke).
- **Contract** (`runner.airtable.fetch_contract(base_id, table, headers)`, also used by `control.ops.fetch_contract`):
  `GET /v0/meta/bases/{base_id}/tables` → the table by id or exact name → an OpenAPI-shaped doc with one operation
  `POST /records` (manifest `sink_call`), whose request body schema has one property per **writable** field name,
  `additionalProperties: false`, no `required`. Types: singleLineText/multilineText/richText/phoneNumber → string;
  email → string `format: email`; url → string `format: uri`; number/currency/percent/rating/duration → number;
  checkbox → boolean; date → string `format: date`; dateTime → string `format: date-time`; singleSelect → string `enum`
  (choice names); multipleSelects → array of enum strings. Computed fields (formula, rollup, lookup, count,
  autoNumber, createdTime, lastModifiedTime, createdBy, lastModifiedBy, button, multipleRecordLinks, attachments)
  are omitted. Each property carries `x-airtable-type`. Hash input: names, types, options, never field ids or
  descriptions, so renaming `Company` → `Employer` changes the hash and `validate_body` reports
  `body.Company: not accepted by destination (no such property)` exactly like a form (`CONTRACT_CHANGED`).
- **send**: `POST /v0/{base_id}/{table}` `{"records": [{"fields": body}], "typecast": false}`, paced ≥ 0.22 s apart per
  base (Airtable: 5 req/s). 200 → record id kept for verify. 422 (`UNKNOWN_FIELD_NAME`, `INVALID_VALUE_FOR_COLUMN`,
  ...) → `SINK_CONTRACT` with Airtable's error type and message; 401/403 → `AUTH`; 429 → wait 30 s, retry once, then
  `UNKNOWN`; other ≥ 400 → `UNKNOWN`. Values are sent typed per the contract (number → JSON number, checkbox → bool,
  date → `YYYY-MM-DD`); a value that does not fit its type is `VALIDATION_FAILED` before any request.
- **verify** (`read_back: true`): `GET /v0/{base_id}/{table}/{record_id}` and compare every sent field (strings
  whitespace-normalised, numbers numerically, dates as dates) → mismatch `VERIFY_MISMATCH`.
- **Dry run**: fetch the contract and validate the body; never POST.
- **TEACH**: `POST /api/integrations/build {recording_id, name?, destination?: {kind: airtable, base_id, table,
  credential}}` retargets a page→form recording to Airtable: each recorded form label maps to the Airtable field with
  the same normalised name; unmatched labels become an `ask_user` in the agent's TEACH, never a guess. `form_url`
  stays as is.
- **REPAIR**: label-keyed sinks (`form`, `airtable`) share the form repair path: `diff_openapi` → `patch_hint`
  (`move /sink/fields/Company → /sink/fields/Employer`) → `migrate_golden_examples` → compile → `request_deploy`.
  The scripted agent does the same.
- **Fake**: `demo_apps/fake_airtable` (FastAPI): meta tables, create, get record, `POST /admin/rename {table, from,
  to}`, `GET /admin/records`, bearer check (`401` on a wrong token), 422 on unknown field names, typed validation.
  `scripts/smoke_v2.py --sink airtable` runs the 2-minute script against the fake (in-process) or the real base
  (`PATCHWORK_AIRTABLE_BASE_ID`, `PATCHWORK_AIRTABLE_TABLE`, credential `AIRTABLE_TOKEN`; the rename is by hand, or via
  the Meta API when the token also has `schema.bases:write`).
- *Added by A2 (additive):* field types not listed above (collaborators, barcodes, AI text, ...) are omitted
  like computed ones; `info` is `{title: <table name>, x-patchwork-kind: airtable, x-airtable-base}`. The compiler
  bakes the mapped fields' types from the snapshot into `adapters.FIELD_TYPES` and `build_body` returns typed
  values (`runner.airtable.convert_values`), so golden bodies hold what is sent; `send` types again against the
  live contract, validates, POSTs and reads back in one step (`verify()` is a no-op, like forms). Runtime
  surface: `Http.airtable_contract(base, table)` / `Http.airtable_submit(base, table, values)` (and
  `FakeHttp`); `control.ops.fetch_airtable_contract(base_id, table, credential, headers=None)`. TEACH: the
  build stores the destination as `destination` in `$WS/recordings/<id>/candidates.json` (a build without one
  clears it); `draft_spec` asks once per unmatched label, options = the table's unmatched writable fields +
  `"Skip this field"` (skip drops the label, recorded as a confirmed assumption); any other answer writes
  nothing. `propose_spec_patch` refuses `/sink/base_id`, `/sink/table`, `/sink/auth`. The fake takes
  `create_app(fields=REAL_BASE_FIELDS)` (no Email, like the real demo base) and `--no-email`.

## V16. SQLite sink (`sink.kind: sqlite`)

A label-keyed sink (V14's shape) whose destination is a table in a local database file. No server, no credential.

- **Spec**: `sink: {kind: sqlite, path: <relative to the workspace>, table: <name>, fields: {<column>: <value source>}, verify: {read_back: true}}`.
  `path` is rejected if absolute, `~`-based or containing `..`; `runner/sqlite_sink.py` resolves it under
  `$PATCHWORK_WORKSPACE` and refuses anything (symlinks included) that leaves it. A missing file is `SINK_CONTRACT`
  and is never created (connections use URI mode `rw`, `ro` for schema reads and dry runs). `auth` is not accepted.
- **Contract** (`runner/sqlite_sink.fetch_contract`): from `PRAGMA table_xinfo`. One operation, `POST /rows`;
  `info["x-patchwork-kind"] == "sqlite"`; one property per writable column (generated and BLOB columns are left out)
  typed by SQLite affinity plus the BOOL / DATE / DATETIME names (`integer`, `number`, `boolean`, `string` with
  `format: date | date-time`), `x-sqlite-type` = the declared type; `required` = NOT NULL without a default (the rowid
  alias is not required). Only names, types and required flags go in, so the hash changes on a renamed or retyped
  column or a new required column, and on nothing else (data, indexes).
- **Write**: values are converted per column (`VALIDATION_FAILED` naming every value that does not fit), validated
  against the contract, then one parameterised INSERT. Column and table names reach the SQL text only after being
  read back from the PRAGMA (a body key that is not a column is refused first) and are double-quote-escaped. Read
  back by rowid: a difference is `VERIFY_MISMATCH`. A dry run reads the schema, validates, and never INSERTs.
- **Errors**: missing file/table/column or read-only database -> `SINK_CONTRACT`; NOT NULL / UNIQUE / CHECK /
  FOREIGN KEY violation -> `VALIDATION_FAILED` (the record is the problem); locked database (10 s busy timeout) or
  anything else -> `UNKNOWN`.
- **Rename -> REPAIR**: `ALTER TABLE ... RENAME COLUMN` changes the contract hash; the run is held before any write
  (`CONTRACT_CHANGED`), `diff_openapi` pairs the rename and proposes a `move` of `/sink/fields/<old>`, a person
  approves, goldens are retyped and the held pages are written. `/sink/path` and `/sink/table` are fixed paths no
  patch may touch. `sqlite` is in `LABEL_KEYED_SINKS`.
- **Demo**: `python -m demo_apps.sqlite_tracker --init | --rename Company Employer | --rows` (default
  `workspace/tracker.db`, table `Applications`).
- Not yet: choosing a SQLite destination in TEACH (`POST /api/integrations/build {destination}` is Airtable-only) and
  the web UI badge.

## V15. Generalization: integrations that are not about jobs

1. **Prompts and tools are domain-neutral.** LEARN/TEACH prompts name the integration's intent and its fields (from the
   spec), never "job title and company". `get_page` lists every JSON-LD object type on the page. Readers written by
   LEARN/TEACH always carry an explicit `jsonld_type`; `save_parser` fills it from the page when missing. The
   runtime default (`JobPosting` when a spec omits it) is unchanged so compiled packages behave as before.
2. **Pages go to one integration.** A page record gains `integration` (nullable). `POST /api/pages` accepts optional
   `integration`; the extension sends the one chosen in its options (dropdown from `GET /api/extension` →
   `{token, control_url, integrations: [{name, intent}], default_integration}`) and the popup can switch it.
   `GET /api/pages?since=&integration=X` returns X's pages plus untagged pages when X is the default integration
   (the first page integration, `jobs_to_tracker` when present); **without** `integration` it returns the default
   integration's pages (so already-compiled `jobs_to_tracker` specs see exactly what they see today). TEACH writes
   `params: {since: "{cursor}", integration: <name>}`. A page triggers runs, learns and tracker rows only for its
   integration.
3. **Tracker rows are generic.** `/api/tracker` rows gain `fields: {<field>: value}` (every extracted field, spec
   order) and `field_names` at the top level; `title`/`company` stay for `jobs_to_tracker` (bench, tests). The
   `VALIDATION_FAILED` explanation names the failing fields.
4. **Demo endpoints take `?integration=`** (default `jobs_to_tracker`): `/api/demo/v2/reset`, `/save-baseline`,
   `/api/layouts`, `/api/tracker`.
5. **New validation rules**: `number` (cast float; optional `min`, `max`), `date` (ISO `YYYY-MM-DD` after the cast or
   transform), `url` (absolute http/https). Implemented in the compiler's validate template and in LEARN's evidence
   check (`agents/learn.py _check_rules`), with generated negative tests.
6. **Default build name** for a page recording: `fixture-jobs` → `jobs_to_tracker` (unchanged); any other recording →
   a slug of its intent (`Track product prices` → `track_product_prices`) unless `name` is given.

**Details fixed while building B1 (additive; nothing above was removed or renamed):**
- Page records (`GET /api/pages`, `GET /api/pages/{id}`) carry `integration` (null = untagged). `pages.db` gains the
  nullable column on first open. `POST /api/pages` answers `integration` = the page's integration (the one sent, else
  the default; null while there is no page integration) and starts a run only for that integration. An `integration`
  that is not a page integration → 422 (`""` = untagged); a non-string → 422.
- `GET /api/extension`: `integrations` are the page integrations in `page_integrations()` order (jobs first) with the
  active spec's `intent`; `default_integration` is null while there is none.
- `GET /api/pages?integration=X` for an unknown X → `[]`. A learn is never started for a page of another integration
  (also guarded in `PageHooks.maybe_learn`, for specs compiled without the param).
- Tracker: `fields` values come from the sink mapping's `{from: <field>}` entries (sink-typed, e.g. strings for a
  form), null when not written yet or mapped only through a transform; `field_names` = the active spec's parser fields.
  The `VALIDATION_FAILED` `detail` is `<field>: <problem>` (e.g. `price: -3 is below the minimum 0`).
- Demo: `?integration=` wins over the body's `integration` on save-baseline; `POST /api/layouts/{id}/revert` also takes
  `?integration=`. jobs_to_tracker's baseline stays in `$WS/v2_baseline/`; others are in
  `$WS/v2_baseline/integrations/<name>/`. A reset clears only that integration's pages (untagged ones too for the
  default) and its learn links.
- Rules: `number` accepts what `cast: float` accepts (commas removed), never booleans/NaN/inf; `min`/`max` are
  inclusive and only allowed on `number` (`min > max` is a spec error). `date` accepts a date object or a
  `YYYY-MM-DD` string of a real day. `url` needs `http`/`https`, a host and no whitespace. Generated negative tests:
  `<f>_not_a_number`, `<f>_below_min`, `<f>_above_max`, `<f>_not_iso`, `<f>_not_a_day`, `<f>_relative`, `<f>_not_http`.
- LEARN: `get_page` adds `json-ld types on this page: …`, the integration's intent, the rules (`price: number (min 0)`)
  and how existing parsers read each field; JSON-LD numbers count as leaves/evidence/corroboration. A `jsonld` reader
  without a type is typed from the page before testing (JobPosting first when the path resolves there, else the first
  type whose first object has the path). The job integration (JobPosting readers, or fields title+company) keeps its
  job learner prompt; any other gets `prompts.learner_prompt(spec)`. The scripted learner tries, for fields other
  than title/company, the integration's own readers, then the path on every JSON-LD type of the page.
- Build name: `deep_tools.intent_slug` (≤ 40 chars, cut at a word, `p_` prefix if it starts with a digit); a page
  recording with no intent still gets `jobs_to_tracker`.
- Extension: `default_popup: popup.html` ("Send pages to" list + "Send this page", which messages the worker's
  `sendActiveTab()`); the toolbar click no longer sends directly. The choice is `integration` in
  `chrome.storage.local` (absent = default); the options page has the same list.

## V17. The document inbox (`/api/documents`, `control/documents.py`)

Files and emails become records the document integrations read, the way pages do (V1). The readers are the
teammate's (`runner.documents.read_any(data, name, content_type) -> Document`); this contract is what the control
plane does with a `Document`. Nothing here knows what a document is about: field names and meaning come from the spec.

### The record

`{id, parent_id, kind, integration, site, name, text, html, tables, meta, flags, sha256, received_at, received_date,
is_leaf}`. Ids are `doc-%06d` (`seq` is an AUTOINCREMENT, so an id is never reused, even after `clear()`).

- `kind`, `name`, `text`, `meta` are copied from the `Document`; `html` is `""` when there is none.
- `tables` is `[{header, rows, where}]`, all cells strings.
- `site` is the routing key (`route_by: site`): the domain of the sender address in `meta["from"]` (bare or
  `Name <addr>`, lower-cased), inherited by an email's attachments; `"upload"` for a file sent by hand. A caller may
  override it (`add_bytes(..., site=)`, used by table connectors).
- A child inherits its parent's `meta` keys that it does not have.
- `flags`, fixed names: `no_text_layer` (from `meta.no_text_layer == "true"`), `encrypted` (from
  `meta.encrypted == "true"`), `unreadable` (the inbox sets it when `read_any` raises, and the adapter sets it when
  a reader marks an attachment with `meta.unreadable == "true"` or a non-empty `meta.error` instead of raising). A flagged document is held by
  `runner.pipeline.process_document` with a plain reason and is **never sent to LEARN**
  (`PageHooks.maybe_learn`). For `unreadable` the error text is in `meta.error` (what the pipeline reads) and
  `meta.problem`.
- `is_leaf`: a child of kind `pdf`, `sheet`, `html` or `text`, or the parent when it has no such child. Only leaves
  are served to the runner; the parent email is kept for audit and is never written as a row.

### The inbox (`DocumentInbox`, same method names as `PageInbox`)

- Store `$PATCHWORK_WORKSPACE/documents.db`; originals under `$PATCHWORK_WORKSPACE/documents/` (never logged).
- `add_bytes(data, name, content_type, integration, received_at=None, site=None, meta=None)` reads, stores the parent
  and children in one transaction and returns the parent record plus `leaves: [ids]` and `duplicate`. The same
  bytes (`sha256`) for one integration return the existing record with `duplicate: true` and store nothing; a unique
  index on `(integration, sha256)` for parents makes two simultaneous uploads one document. The insert is one
  transaction (`BEGIN IMMEDIATE`), so a parent never exists without its leaves. The stored file name is bounded (no
  directory part, no NUL, at most 100 characters on disk).
- `list(since, limit, integration, leaves_only=True)`, `get`, `summaries`, `count`, `clear(integration=None)`,
  `add_learn`/`learns` (learn threads of documents, keyed by document id).
- `control.inboxes.inbox_for(record_id)` returns the page inbox for `pg-…` and the document inbox for `doc-…`
  (`ToolContext.get_page_record` uses it).

### API (`control/api/documents.py`)

- `POST /api/documents`: multipart `file` + `integration`, token as for `POST /api/pages` (401), at most 25 MB (413).
  The multipart body is parsed with the standard library (no extra dependency).
  - 201 with `{id, kind, name, site, flags, meta, sha256, integration, leaves, duplicate, run_id, queued}`; 200 when
    the bytes were already there (no second run). No body text is returned.
  - 422 when `integration` is missing or names neither a document integration nor a wizard draft; 422 for a missing
    `file` or a non-multipart body; 503 when `runner.documents` is not importable (the upload is not lost: nothing is
    stored, so it can be sent again).
  - A new leaf starts a run (`AutoRunner.request`), except for a wizard draft, whose documents wait.
- `GET /api/documents?since=&integration=&limit=&leaves_only=true`: the runner's source (no token: localhost only).
- `GET /api/documents/{id}`; `GET /api/documents/stream?after=&integration=`: SSE `document` events, one per new
  leaf, without `text`, `html` or `tables`.

### Specs

`control.autorun.is_document_spec(spec)`: `source.base_url == patchwork://control` and `source.list.path ==
/api/documents`. `is_page_spec` is the same source without that path, so a document spec is never a page spec.
`is_inbox_spec` / `is_inbox_integration` mean either; run-on-arrival, learn triggers and the replay after a deploy
(`PageHooks`) use the inbox variants.

## V18. Rows, `clean`, cross-check rules, per-row records (generalized input)

One input document (an HTML table page, a CSV/XLSX sheet, the text lines of a PDF) becomes many checked records. All
additions are optional: a spec without `rows`, `clean` or a cross-check rule validates, dumps, hashes and compiles
exactly as before. Field names come only from the spec; the code knows no subject.

### Spec (`spec/models.py`)

- **`Parser.rows: Rows | None`**. `Rows = {select, fields, require?, hold?}`:
  - `select`, exactly one of:
    - `{table: {columns: [<header>, ...]}}` (`columns` not empty): the first table whose header contains every name
      (whitespace-normalised, case-insensitive).
    - `{css: "<selector>"}`: each match in the record's HTML is a row element (needs `source.html_field`).
    - `{lines: {after?, before?, match, skip?}}` (regexes): the text lines strictly between the first line matching
      `after` and the next line matching `before`. A line that `match`es is a row; its named groups are the row's
      cells. A line `match` misses must be blank, a repeat of a line from before the window (above and including
      the `after` line), or matched by `skip`.
  - `fields: {<ident>: RowExtract}` (not empty). `RowExtract` = exactly one of `column` (a table header or a named
    group of `lines.match`, case-insensitive), `index` (a cell or group position, from 0), `css` (inside the row
    element; optional `attr`) or `regex` (on the row's text, group 1); a `regex` next to `column`/`index`/`css` is a
    refine. Plus `cast` (`str` default) and `clean`. `column`/`index` need `select.table` or `select.lines`; `css`
    needs `select.css`; a `column` with `select.lines` must be a named group of `match`.
  - `require: [<row field>, ...]`: a subset of `fields`; when omitted it is filled with `[<first row field>]`.
  - `hold: document | row` (default `document`).
- **`Parser.all_fields()`** = document field names | row field names. A name in both is an error.
- **Either every parser has `rows` or none does** (`Spec.has_rows()`). Field references (validate, dedupe, sink) are
  checked against the intersection of `all_fields()`. `record.row_no` (from 1) and `record.row_count` are record
  refs, valid like any `record.*`.
- **`clean`** on `FieldExtract` and `RowExtract`: `money` | `{money: {decimal: "." | ","}}` | `number` |
  `{number: {decimal}}` | `{date: [<strptime formats>, ...]}` (not empty) | `{split: {sep, part}}` | `strip` | `upper` |
  `lower` (`runner.clean.CLEANERS`). Anything else is a spec error. `clean` runs on the raw value before `cast`.
- **`FieldExtract`**: a `regex` next to `json_path` is now a refine (as next to `css`/`jsonld`), e.g.
  `{json_path: "$.meta.subject", regex: 'Ref\s+(\S+)'}`.
- **Cross-check rules** in `validate` (`spec.models.CROSS_RULES`), allowed only when every parser has `rows`:
  - `{rule: sum_equals, rows_field: f, field: g, tolerance?}`: Σ of row field `f` equals document field `g`.
  - `{rule: product_equals, fields: [a, b], field: c, tolerance?}`: on every row, `a × b` equals row field `c`.
  - `{rule: count_equals, field: g}`: the number of rows equals document field `g`.
  - `tolerance` defaults to `0.01` on these rules and is refused on any other rule (so other rules dump as before).
    `rows_field` and `fields` must be row fields common to every parser; `field` is a document field (a row field for
    `product_equals`).

Two examples (one with document fields and a cross-check, one without):

```yaml
extract:
  route_by: site
  parsers:
    - id: sender_a
      match: {site: sender-a.example}
      fields:
        ref:   {json_path: "$.meta.subject", regex: 'Ref\s+(\S+)'}
        total: {regex: 'Total\s+(\S+)', clean: money}
      rows:
        select: {lines: {after: '^Item\s+Qty', before: '^Total', match: '^(?P<item>.+?)\s+(?P<qty>\d+)\s+(?P<amount>\S+)$'}}
        fields:
          item:   {column: item}
          qty:    {column: qty, cast: int}
          amount: {column: amount, clean: money}
validate:
  - {rule: sum_equals, rows_field: amount, field: total}
dedupe: {key: [ref, record.row_no]}
```

```yaml
      rows:
        select: {table: {columns: [Name, Email, Session]}}
        hold: row
        fields:
          name:    {column: Name}
          email:   {column: Email, clean: lower}
          session: {column: Session}
```

### Rows engine and cleaners (`runner/rows.py`, `runner/clean.py`)

One implementation, imported by compiled parsers and by the agents' checks. Both take plain dicts (the spec's
`rows` block, `model_dump(exclude_none=True)` or hand-written), so a candidate can be tested without a model. Pure:
stdlib and `runner/` only, no network, no AI, no subject words.

- **`runner.clean.clean_value(value, spec, where) -> Any`** raises `Quarantine("VALIDATION_FAILED", "<where>: ...")`:
  - `money` → float. Strips currency symbols (Unicode `Sc`) and a leading or trailing 3-letter ISO code. `(x)`, a
    leading `-` or a trailing `-` make it negative (two signs are refused). With `decimal: "."` (default) `,` groups
    thousands in groups of 3 only; with `decimal: ","` the marks swap. Anything else is refused, and the detail
    names the other reading: `'1.299,00' is not a money amount with decimal '.'; with decimal ',' it would read
    1,299.00`. A group never starts with 0 (`0,125` is not grouped). One mark followed by exactly 3 digits with no
    grouping (`€1.299`, or `1,299` with `decimal: ","`) reads two ways, so it is refused too:
    `'€1.299' is ambiguous: 1.299 with decimal '.' or 1,299.00 with decimal ','`.
  - `number` → int when written without a fraction, else float; the same grouping rules, no currency, no `(x)`.
  - `{date: [formats]}` → `YYYY-MM-DD`. Every format is tried: none fits → `does not match any of the date formats`;
    two that read differently → `'03/04/2026' is ambiguous: 2026-03-04 or 2026-04-03` (never a guess).
  - `{split: {sep, part}}` → the stripped part (`part` from 0; a missing part is refused). `strip`, `upper`, `lower`.
- **`runner.clean.clean_cast(value, spec, to, where)`**: `clean` then `cast`. With a cleaner, `cast: str` (the
  default) keeps what the cleaner returned (a money value stays a float); `cast: int` needs a whole number.
- **`runner.clean.parse_amount(value) -> Decimal | None`**: a lenient, never-raising read of a numeric cell (used to
  recognise aggregate rows).
- **`runner.rows.Row`**: `no` (from 1, over the kept rows), `text`, `cells: {header or group name: text}`, `fields`.
- **`runner.rows.select_rows(record, select, html_field="html", text_field="text", where="rows")`** → `[(text,
  cells)]` for a table (cells by header), `[(text, element outer HTML)]` for css, `[(line, groups)]` for lines.
- **`runner.rows.extract_rows(record, rows, where, html_field="html", text_field="text", skipped=None) -> [Row]`**:
  - Tables come from `record["tables"]` (V17 `{header, rows, where}` dicts) when the key is present, else from the
    teammate's `runner.documents.read_html(record[html_field], record.get("url", "")).tables` (a page record).
    Cells and headers are whitespace-normalised; short rows are padded with `""`.
  - A row's text is its non-empty cells joined by one space (table), the element's text (css) or the stripped line.
  - Row fields: `column` / `index` read a cell (a lines `column` reads a named group); `css` the first match inside
    the row element; `regex` group 1 of the row's text (on a lines row, a regex-only field named like a group of
    `match` reads that group). An empty or unmatched value is `None`. A refine that misses is `PARSE_NO_MATCH`; a
    value `clean`/`cast` refuses is `VALIDATION_FAILED` / `PARSE_NO_MATCH` naming `<where>: row N <field>`.
  - A table row equal to the header is skipped ("header"). A row whose `require` fields are empty is skipped. Every
    skipped row must pass `explain_skip`, else `PARSE_NO_MATCH "<where>: row N skipped but not blank, a header or an
    aggregate: '<text>'"` (N counts the selected rows from 1). Zero kept rows → `PARSE_NO_MATCH`.
  - A kept row that is the aggregate of the other kept rows (at least 2 others; `explain_skip`'s rule) holds the
    document: `PARSE_NO_MATCH "<where>: row N ('<text>') adds up the other rows; a total is not a data row ..."`.
    This catches a total whose label sits in a required column. A real row that equals the sum of the others is
    held too (a person confirms it); it is never dropped.
  - `lines`: `after` / `before` that match no line → `PARSE_NO_MATCH`. A line between them that `match` misses must
    be blank, equal (normalised) to a line *before the window* (above and including the `after` line; nothing
    when there is no `after`), or matched by `skip`; else `PARSE_NO_MATCH "<where>: line '<text>' is not a row and
    not explained"`. This holds for lines above the first matched row too.
  - With a `skipped` list, every skipped row (`{row, text, reason}`) and explained line (`{line, text, reason}`) is
    appended, for the agents' "skipped and explained" report.
  - A missing table → `no table has the columns [...]`; a `column` not in the chosen table's header → `PARSE_NO_MATCH`.
- **`runner.rows.explain_skip(row_text, cells, header, kept) -> "blank" | "header" | "aggregate" | None`**: aggregate
  = at least one numeric cell, and every numeric cell equals the sum of its column over the kept rows (each kept
  cell numeric) within 0.01.
- **`runner.rows.check_cross(rule, doc_fields, rows) -> str | None`** (`Decimal`; tolerance default 0.01): `None` when
  the rule holds, else e.g. `sum_equals: the rows' amount add up to 3,205.00 but total is 3,215.00`,
  `product_equals: row 2: qty × price = 3.75 but amount is 3.00`, `count_equals: 2 rows but n is 3`, or `... is not
  a number: <value>`.

### Runtime: one document → many records (`compiler/`, `runner/pipeline.py`, `runner/run.py`, `runner/store.py`)

- **Compiled package.** Everything below is generated only under `has_rows` (every parser has `rows`). A spec without
  `rows` compiles to byte-identical files (a doc-level `clean` only adds a `clean_cast` import and call).
  - `parsers.py`: `ROWS_<ID> = {<the rows block as a literal dict>}`; `parse_<id>(record)` returns the document
    fields plus `"__rows__": [runner.rows.Row]` from `extract_rows(record, ROWS_<ID>, '<id>.rows', HTML_FIELD,
    TEXT_FIELD)`. When `extract_rows` raises, the exception carries the document fields (`q.fields`) and, for an
    unexplained skipped row, the kept rows (`q.rows`).
  - `integration.py`: `EMITS = "rows"`, `HOLDS = {<parser id>: "document" | "row"}`, `CROSS_RULES = [<rule dicts>]`
    and `validate_document(fields, rows)`, which raises `VALIDATION_FAILED` with `check_cross`'s detail on the first
    failing rule. `validate(fields, record, http)` is unchanged (cross-check rules are not in it) and runs per row.
    Packages without rows have no `EMITS`.
  - `manifest.json` gains `"emits": "rows"` (absent otherwise).
  - Golden examples of a rows spec use `expect: {rows: [<body>, ...]}` (one body per kept row, in row order) or
    `expect: {quarantine: <code>}`; `expect: {body}` is refused for a rows spec and `rows` for any other. The
    generated tests (`test_golden`, `test_dedupe`, `test_negative`, `test_contract`) go through `process_document`;
    negative cases mutate the first row's merged fields. Preflight's contract check also covers every row body.
- **`runner.pipeline.process_document(integ, record, http, store) -> list[Outcome]`:**
  1. Any `flags` entry → one `PARSE_NO_MATCH` Outcome with the document's id, never parsed: `no_text_layer` → "no
     text layer: a scanned or image-only PDF; Patchwork reads text PDFs only", `encrypted` → "encrypted: the
     document is password-protected; Patchwork cannot read it", `unreadable` → "unreadable: the document could not
     be read" (plus `(meta.error)` when present); any other flag → "flagged <flag>".
  2. Route and parse; a failure → one Outcome with the document's id (`parser` unset, `routed_by` set, as in
     `process_record`). An unexplained skipped row whose kept rows also fail a cross-check is reported as the
     cross-check: `VALIDATION_FAILED "<cross-check detail>; and <the unexplained row>"`.
  3. `validate_document`; a failure → one `VALIDATION_FAILED` Outcome with the document's id. Nothing is written.
  4. Per row: `rec_row = {**record, "row_no": n, "row_count": len(rows)}`, merged fields = document fields + row
     fields; `validate`, then `dedupe_key`. Two rows of one document with the same key are `VALIDATION_FAILED` ("the
     dedupe key must tell the rows apart"), never a silent duplicate. A key already processed → `duplicate`;
     otherwise `build_body`.
  5. `hold: document` (default): any failure in step 4 → one Outcome with the document's id (`row N: <detail>`), and
     nothing is sent. Every row is validated and its body built before the first send. `hold: row`: that row's
     Outcome is held and the others go on.
  6. Each ready row is sent and verified → Outcome `"<id>#<row_no>"` (`success`, or held with the sink's reason).
     Send failures are per row (each row is its own write; a replay re-sends only rows not yet processed).
  Every detail is redacted (V13). `process_record` is unchanged.
- **`runner.run.run`** calls `process_document` when `getattr(integ, "EMITS", None) == "rows"`, else
  `process_record`. Every Outcome is stored (`records` table) with `raw` = the document; the run log gets one
  `record` event per Outcome; counts are per Outcome. The cursor moves once per document. The circuit breaker counts
  consecutive `SINK_CONTRACT` Outcomes and is checked after each document. `NEW_SOURCE_FORMAT` counts only
  document-level (or record-level) `PARSE_NO_MATCH` Outcomes, as before.
- **Outcomes per document.** `runner.store.document_key(record_id)` maps `<id>#<n>` to `<id>` (any other id is its
  own document). `Store.latest_outcomes_by_document(name, id_field="id")` groups the latest live outcomes by document
  (the raw record's `id_field` when it has one, else `document_key`) and keeps only the outcomes of the document's
  **latest run**: rows written later supersede an earlier document-level hold, and a later hold supersedes earlier
  rows. `summarize_document(rows)` turns one document's outcomes into one record with the document's id: `status`
  `quarantined` (with the first held row's reason, and detail `row <n>: …`) when any row is held, else `duplicate`
  when every row is a duplicate, else `success`, plus `rows: {success, duplicate, quarantined}`; a plain record
  (`<id>`) is returned unchanged, so one-record pages and v1 read exactly as before.
  `Store.latest_document_outcomes` is that, per document (the bench accuracy endpoint scores it).
- **The tracker** (`control/tracker.py`) reports a page's row from `summarize_document`: a page whose rows were all
  written is `success` (label `code`, or `learned_now` when the parser was learned from it), all duplicates is
  `duplicate`, any held row or a document-level hold is `held`; the tracker row gains `rows: {success, duplicate,
  quarantined}` for a document with rows (absent otherwise).
- **`Store.quarantined_raw(name, id_field="id")`** (the replay input) uses `latest_outcomes_by_document` and returns
  each document once when any outcome of its latest run is quarantined. `runner.run.run` passes
  `id_field=integ.RECORD_ID` (the spec's `source.record_id`). Records whose raw has no such field are grouped by
  `document_key`.
