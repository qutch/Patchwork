# Patchwork v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show a page → form transfer once; afterwards a hotkey on any job posting writes a row into the demo Google
Form/Sheet with zero AI calls for known layouts, a checked and labelled one-pass learn for new layouts, and a
blocked-before-write repair when the form changes.

**Architecture:** v1's spec → compiler → AI-free runner spine gains page extractors (`css`, `jsonld`, regex refine),
fingerprint routing and a `kind: form` sink filled by headless Playwright, with an OpenAPI-shaped form contract so v1's
preflight/incident/repair machinery is reused. A Chrome extension posts pages to a page inbox on the control plane, which
runs the `jobs_to_tracker` integration on arrival and starts a LEARN thread (deepagents) for unknown layouts.

**Tech Stack:** Python 3.12, Pydantic v2, Jinja2, selectolax 1.0.2 (lexbor), Playwright 1.65.2, FastAPI, SQLite,
deepagents 0.7.38 + langchain-openrouter 0.2.9, React/Vite/TS/Tailwind, Chrome MV3.

**Spec:** `docs/superpowers/specs/2026-11-14-patchwork-v2-design.md` (source of truth). **Contracts:** `docs/contracts.md`
Part 2 (V0–V12). Every task implicitly includes the contract sections it names.

## Global Constraints

- All AI work uses LangChain Deep Agents: `deepagents==0.7.38` via `agents/deep.py` `DeepAgentService` (`create_deep_agent`
  supervisor, subagents through `task`, `write_todos` via `TodoListMiddleware`, `FilesystemMiddleware` with writes limited to
  `/spec.yaml`, `/examples.yaml`, `/transforms.py`, plus `EventMiddleware` and `ResilientModelMiddleware`). TEACH, LEARN and
  REPAIR are modes of that one agent; LEARN adds a `learner` subagent. Guardrails live in tools (`agents/deep_tools.py`), never only in prompts.
- `ScriptedAgentService` gets matching scripts only as the stage fallback, emitting the same events.
- The runner stays AI-free: subprocess, stripped env, no model keys, refuses to start with AI modules loaded.
- Never silently write a wrong row; when in doubt hold the record for a person. Every AI-produced value is checked by code and labelled in the UI.
- The runner must keep running packages already compiled by the v1 templates (the owner's live demo may run them
  through this checkout's runner); new template outputs may differ.
- v1 (`po_to_stockroom`) keeps working unchanged; `.venv/bin/python -m pytest -q` stays at ≥ 136 passed (2 skipped) plus
  new tests; `cd web && npm test` ≥ 34 passed; `npm run typecheck` and `npm run build` clean.
- Never bind, kill or reset ports 8000–8003 or 5180. Use free ports, in-process servers and a separate `PATCHWORK_WORKSPACE`.
- OpenRouter key lives in the gitignored `.env`: never print, log or commit it. Free tier 20 req/min, 1,000/day, shared.
  Default to the scripted agent and offline fake models; live-call budgets in V0.
- Develop against the demo Form/Sheet (or the fixture tracker form), never the owner's real tracker. Automated tests never
  submit to Google; only the integrator's rehearsals and opt-in live checks do.
- Commit on your workstream branch; the integrator merges into `project-roadmap-mvp-68d423de`. No pushes, no PRs. Every
  commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Python: `/Users/hutch/.paradev/worktrees/hack-princeton/through-plan-create-68d423de/.venv/bin/python` (absolute; new
  worktrees have no venv). Web: run `npm ci` in your worktree's `web/` once.

Demo targets and what both spikes established: `docs/contracts.md` V0 and V5–V8 (the demo Form's labels are `Email`,
`Job Title`, `Company`, `Link`, `Date Applied`; 5/5 headless submissions landed in the Sheet; ~1.3–2 s per submission).

## Review Focus

1. **The same posting sent twice with a different query string or `#fragment`** → one row, the second press shows
   `duplicate`. Test: Task W4a-2 `test_same_posting_twice_is_duplicate`, Task W1-6 `test_rerun_writes_nothing`.
2. **A form label that appears twice, differs by case/`*`/whitespace, or vanished** → fill the right control or fail closed
   with `SINK_CONTRACT`, never fill a neighbour. Test: Task W1-5 `test_label_resolution_fails_closed`.
3. **Two hotkey presses while a run is in progress** → both pages processed exactly once (one queued follow-up run).
   Test: Task W4a-2 `test_presses_during_run_are_queued_once`.
4. **A learned reader that picks page chrome** (cookie banner, "Apply for this job", the description's second `h1`) →
   never auto-used: ambiguity or corroboration fails, a person decides. Test: Task W3-2 `test_page_chrome_is_not_corroborated`,
   Task W3-3 `test_save_parser_asks_when_not_corroborated`.
5. **AI switched off or the model down when an unknown layout arrives** → the row is `held` with reason `PARSE_NO_MATCH`,
   no learn thread, nothing written; switching AI on and replaying learns it. Test: Task W4a-3 `test_unknown_layout_with_ai_off_is_held`.

---

## Workstream map

| # | Workstream | Branch | Depends on (builds against contracts + fakes until merged) |
|---|---|---|---|
| W0 | Foundation (integrator, before dispatch) | main branch | — |
| W1 | Engine | `v2-engine` | W0 |
| W2 | Capture + extension | `v2-capture` | W0; W4a's `/api/pages` (stub server in tests) |
| W3 | Agents | `v2-agents` | W0; W1's spec models (tests use `fixtures/jobs/` once W1 lands; until then a local copy of the V7 spec) |
| W4a | Control plane | `v2-control` | W0; W1 (fake compiled integration in tests), W3 (`start_learn` via a fake agent) |
| W4b | UI | `v2-ui` | V9 only (mock server) |
| W5 | Proof + pitch | `v2-proof` | W0; full stack at integration |

Merge order: W1 → W3 → W4a → W2 → W4b → W5, full suites after each (Task I-1).

---

## W0. Foundation (integrator; done before dispatch)

### Task W0-1: shared page helpers

**Files:** Create `runner/html.py`; Modify `runner/lib.py` (add `extract_css`, `extract_jsonld`, `refine_regex`); Test `tests/engine/test_html.py`.

**Interfaces:** Produces V2's functions and V3's runtime helpers exactly as in `docs/contracts.md`.

- [ ] **Step 1: Write failing tests** in `tests/engine/test_html.py`, driven by `fixtures/pages/index.json`:
  `test_expected_values_extract` (the four spike readers give `expected` on all 8 pages, via `extract_css`/`extract_jsonld`/`refine_regex`),
  `test_site_and_canonical` (`site_of("https://www.Jobs.Lever.co:443/x")=="jobs.lever.co"`; `lever-source`, `utm_*`, `#apply` removed),
  `test_visible_text_skips_script_and_style` (no `{` from the Lever inline style; expected title in text on all 8),
  `test_ashby_h1_is_ambiguous` (`len(set(css_all(ashby, "h1"))) == 3`), `test_bad_html_never_raises` (`css_all("<<<", "h1") == []`, `jsonld_objects("<script type=application/ld+json>{bad") == []`),
  `test_misses_quarantine` (`extract_css(html, ".nope", None, "p.f")` raises `Quarantine` with code `PARSE_NO_MATCH`).
- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/engine/test_html.py -q` → FAIL (module missing).
- [ ] **Step 3:** Implement with `selectolax.lexbor.LexborHTMLParser`; `normalize_ws` = `" ".join(s.split())`.
- [ ] **Step 4:** Run → PASS; run the full suite → 136+ passed.
- [ ] **Step 5:** Commit `Foundation: runner/html.py page helpers and css/jsonld/refine extract helpers`.

---

## W1. Engine (spec / compiler / runner / fixture form)

### Task W1-1: spec models for page extractors, routing and form sinks

**Files:** Modify `spec/models.py`, `spec/__init__.py`; Create `fixtures/jobs/spec.yaml`, `fixtures/jobs/examples.yaml`; Test `tests/engine/test_spec_v2.py`.

**Interfaces:**
- Produces: `FieldExtract.css|attr|jsonld|jsonld_type` (+ `regex` as refine), `Source.html_field`, `Parser.fingerprint`,
  `Parser.origin: Origin | None` (`kind: Literal["taught","learned","fixture"]`, `at: str | None`, `thread_id: str | None`,
  `approved_by: Literal["auto","human"] | None`), `Sink = Annotated[ApiSink | FormSink, Field(discriminator="kind")]`,
  `FormSink(kind="form", url: str, fields: dict[str, ValueSource], submit: str = "Submit", verify: FormVerify | None)`,
  `FormVerify(confirmation_text: str)`. `Spec.sink_values() -> dict[str, ValueSource]` (body or fields) used by every
  reference check and `custom_transforms()`.
- `fixtures/jobs/spec.yaml` is V7 + V5 with three `origin.kind: fixture` parsers (lever, greenhouse, ashby; the spike
  readers, fingerprints `.posting-headline`, `[src*=".greenhouse.io/"]`, `[class*="ashby-job-posting"]`) and the fixture
  tracker form URL `http://127.0.0.1:18010/` (tests override). `examples.yaml`: one golden per fixture page of those three platforms.

- [ ] **Step 1: Failing tests:** `test_jobs_fixture_validates`; `test_v1_spec_hash_unchanged` (hash of `fixtures/spec.yaml`
  equals the value printed by the current code before you change anything; paste it as a literal);
  `test_extractor_errors` (parametrized, each yields one readable error mentioning the field: `css` without
  `source.html_field`; `attr` without `css`; `jsonld_type` without `jsonld`; `combine` with `css`; refine regex with 0
  groups; `css` + `json_path` together); `test_form_sink_reference_checks` (`fields: {Company: {from: employer}}` →
  error naming `sink.fields.Company`); `test_fingerprint_needs_html_field`.
- [ ] **Step 2:** Run `pytest tests/engine/test_spec_v2.py -q` → FAIL.
- [ ] **Step 3:** Implement. Keep every v1 field name and error text; new optional fields default to `None` so
  `spec_to_dict(exclude_none=True)` and `spec_hash` of v1 specs don't change.
- [ ] **Step 4:** Run → PASS; `pytest tests/test_spec.py tests/test_compiler.py -q` → PASS.
- [ ] **Step 5:** Commit.

### Task W1-2: compiled extractors, fingerprint routing, `routed_by`

**Files:** Modify `compiler/templates/parsers.py.j2`, `compiler/compile.py` (manifest `parsers`), `runner/pipeline.py`
(`Outcome.routed_by`), `runner/store.py` (`records.routed_by` column via `_add_column`); Test `tests/engine/test_compile_pages.py`.

**Interfaces:** Consumes W0-1 helpers. Produces `route(record) -> (parser_id, fn, routed_by)` in generated `parsers.py`
and `extract(record) -> (parser_id, fields, routed_by)` in generated `integration.py`. The pipeline accepts both the
3-tuple and v1's 2-tuple (`routed_by = None`), because packages already compiled by v1 templates must keep running.

- [ ] **Step 1: Failing tests:** `test_fixture_pages_extract` (compile `fixtures/jobs`, load, `extract` on the 6
  lever/greenhouse/ashby pages equals `index.json` expected, `routed_by == "match"`);
  `test_branded_page_routes_by_fingerprint_then_holds` (careers.airbnb.com page → routed to `greenhouse` by fingerprint,
  outcome `PARSE_NO_MATCH`, `routed_by == "fingerprint"`); `test_unknown_site_no_fingerprint` (a page with
  `site: example.org` and plain HTML → `PARSE_NO_MATCH` detail contains `no fingerprint matched`);
  `test_manifest_lists_parsers_with_origin`; `test_v1_routing_unchanged` (v1 fixture spec: same outcomes on the seed as before, via existing `tests/test_runner.py`).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement (parse each record's HTML once per record). **Step 4:** Run → PASS + full suite. **Step 5:** Commit.

### Task W1-3: fixture tracker form app

**Files:** Create `demo_apps/tracker_form/{__init__,__main__,app}.py`; Test `tests/engine/test_tracker_form.py`.

**Interfaces:** V8 exactly. Mimic the markup facts spike (a) records in `fixtures/google_form/` (label linkage, required
marker, submit name, confirmation text).

- [ ] **Step 1: Failing tests:** `test_form_matches_demo_form` (labels `Email, Job Title, Company, Link, Date Applied`;
  only Email required; Email input `aria-label="Your email"`), `test_submit_stores_row_and_confirms`
  (confirmation page contains `Your response has been recorded.`), `test_rename_changes_label`, `test_reset`,
  `test_missing_required_rejected` (no row, form re-shown).
- [ ] **Steps 2–5:** run → FAIL, implement, run → PASS, commit.

### Task W1-4: `runner/forms.py` (snapshot, contract, fill and submit)

**Files:** Create `runner/forms.py`; Test `tests/engine/test_forms.py`.

**Interfaces:** V5/V6 signatures. `fill_and_submit` raises `Quarantine` codes per V5.

- [ ] **Step 1: Failing tests** (fixture form on a free port, headless Chromium):
  `test_snapshot_fixture_form` (5 labels; only `Email` required; `input == "text"`);
  `test_contract_hash_stable` (two `fetch_form_contract` calls, equal `openapi_hash`);
  `test_rename_breaks_contract_with_exact_errors` (after `/admin/rename Company→Employer`, `validate_body` on a golden body
  returns exactly `["body.Company: not accepted by destination (no such property)"]`; with a fixture form whose Employer is
  required it also returns `"body.Employer: required by destination but missing from body"`);
  `test_fill_and_submit_writes_one_row`; `test_dry_run_never_submits` (0 rows);
  `test_label_resolution_fails_closed` (parametrized: label missing → `SINK_CONTRACT`; a form with two controls labelled
  "Company" → `SINK_CONTRACT`; "Job title" resolves "Job Title"; "Email" resolves the `aria-label="Your email"` input
  through its heading; the "Clear form" button is never clicked); `test_no_confirmation_is_verify_mismatch`;
  `test_google_form_snapshot_offline` (`page.set_content` of `fixtures/google_form/viewform.html` → labels
  `["Email", "Job Title", "Company", "Link", "Date Applied"]`, Email required). Reference implementation from spike a:
  `scripts/spike_google_form.py` (`snapshot_form`, `resolve_control`, `fill_and_submit`).
- [ ] **Steps 2–5:** run → FAIL, implement, run → PASS, commit.

### Task W1-5: form adapter, form preflight, `patchwork://control`, `fetch_contract`

**Files:** Create `compiler/templates/adapters_form.py.j2`; Modify `compiler/compile.py` (choose adapter by `sink.kind`;
manifest `contract_kind`, `sink_call` `POST /form`), `compiler/templates/integration.py.j2` (`fetch_contract`, `send` for
form takes the run's browser page), `runner/run.py` (preflight via `integ.fetch_contract(http)`; one browser per form run;
`--dry-run` for form sinks needs no staging URL), `runner/http.py` (V7 resolution), `control/ops.py` (`fetch_contract`,
`fetch_sink_openapi` kept as an alias, `compile_and_test` uses it), generated tests templates where they assume `body`;
Test `tests/engine/test_jobs_e2e.py`.

**Interfaces:** Produces the full V5–V7 runtime. Generated form adapter exports `list_records(http, cursor)`,
`fetch_contract(http) -> dict`, `send(http, body) -> FormResult` (`status_code` 200 on confirmation), `verify(...)` (no-op;
confirmation is checked inside `send`). The pipeline's `_sink_status` keeps working.

- [ ] **Step 1: Failing tests** (in-process fake page inbox serving `/api/pages` from `fixtures/pages`, fixture form,
  `PATCHWORK_CONTROL_URL` set, temp workspace): `test_jobs_run_end_to_end` (6 `success`, 2 `PARSE_NO_MATCH`, 6 form rows,
  summary `llm_calls == 0`); `test_rerun_writes_nothing` (second run: 0 new rows, a page re-posted with `?utm_source=x#top`
  → `duplicate`); `test_rename_blocks_before_any_write` (status `blocked`, `CONTRACT_CHANGED` incident with V6 errors and
  `old_openapi`/`new_openapi`, row count unchanged); `test_dry_run_form_writes_nothing`;
  `test_global_overrides_do_not_leak` (`PATCHWORK_SOURCE_BASE_URL`/`PATCHWORK_SINK_BASE_URL` set to junk: jobs run still
  succeeds); `test_control_url_missing_fails_clearly`; `test_runner_still_refuses_ai_modules` (existing check covers forms path).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS; full suite (all v1 tests) → PASS.
- [ ] **Step 5:** Commit.

### Task W1-6: opt-in live check against the demo Google Form

**Files:** Test `tests/engine/test_google_form_live.py` (`@pytest.mark.live`, skipped unless `PATCHWORK_DEMO_FORM_URL`).

- [ ] **Step 1:** `test_live_snapshot` (labels match the five expected) and `test_live_dry_run_fill` (fills, never
  submits). No submissions from tests, ever.
- [ ] **Step 2:** Run with the env var set once; report the result. **Step 3:** Commit.

---

## W2. Capture + extension

### Task W2-1: element paths and page snapshots on copy

**Files:** Modify `capture/recorder.js`, `capture/recorder.py` (save `snapshots/<n>.html`, bare-host allowlist);
Test `tests/capture/test_recorder_pages.py`.

**Interfaces:** V11 recorder additions; existing event shapes unchanged.

- [ ] **Step 1: Failing test** `test_copy_records_path_and_snapshot`: headless recording against a local static server
  serving `fixtures/pages/lever-1.html` and the fixture tracker form (W1-3; until merged, a 20-line stub form in the test):
  select + copy the `h2`, paste into "Job Title", submit. Assert the copy event has `el.path` such that
  `runner.html.css_all(snapshot_html, el.path) == ["American Tech Fellowship"]` and `snapshots/1.html` exists.
  `test_bare_host_allowlist` (allowlist `127.0.0.1` records any port on it).
- [ ] **Steps 2–5:** run → FAIL, implement, run → PASS (plus all `tests/capture`), commit.

### Task W2-2: page → form candidates

**Files:** Modify `matcher/core.py` (+ new `matcher/pages.py` if cleaner); Test `tests/matcher/test_page_form.py`.

**Interfaces:** Produces V11 `candidates.json` for page→form recordings; `matcher.match(recording)` signature unchanged.

- [ ] **Step 1: Failing tests** on `fixtures/recording_jobs/recording.json` (Task W2-3 makes it; write W2-3's script first
  if needed): `test_sink_is_form` (`kind == "form"`, 4 labels, submit, confirmation text); `test_title_mapping_is_css`
  (`css_first(snapshot, mapping["css"]) == mapping["value"]`, `copied: true`, `alternatives` contains `{"jsonld": "title"}`);
  `test_link_and_date_are_record_fields`; `test_v1_recordings_unchanged` (existing `tests/matcher` pass).
- [ ] **Steps 2–5:** run → FAIL, implement, run → PASS, commit.

### Task W2-3: fixture recording `fixture-jobs`

**Files:** Create `scripts/make_recording_jobs.py`, `fixtures/recording_jobs/{recording.json,candidates.json,snapshots/1.html}`.

- [ ] **Step 1:** Script drives the real recorder headless over `lever-1.html` → fixture form (copy title, copy company,
  paste both, type `demo@example.com`, the link and today's date, submit). **Step 2:** Run it; commit its outputs; re-running produces the same
  mappings (test `test_fixture_recording_is_reproducible` compares mappings, ignoring timestamps). **Step 3:** Commit.

### Task W2-4: Chrome MV3 extension

**Files:** Create `extension/{manifest.json,background.js,options.html,options.js,README.md,icons/}`; Test `tests/extension/test_extension.py`.

**Interfaces:** V11 extension contract; consumes V9 `POST /api/pages`, `GET /api/pages/{id}`.

- [ ] **Step 1: Failing tests** (Playwright persistent context with `--load-extension`, headless; a stub control plane
  on a free port): `test_send_page_posts_dom_and_token` (call the service worker's exported `sendActiveTab()` via
  `context.service_workers[0].evaluate`; the stub received `url`, `title`, `html` containing the page's `<h2>`, header
  `X-Patchwork-Token`); `test_badge_reflects_outcome` (stub row label `code` → badge `✓`; `learned_now` → `AI`; `held` → `!`);
  `test_options_persist` (controlUrl + token in `chrome.storage.local`).
- [ ] **Steps 2–4:** run → FAIL, implement, run → PASS. **Step 5:** `extension/README.md`: Load unpacked, set the shortcut
  at `chrome://extensions/shortcuts`, paste the token from the UI. Commit.

---

## W3. Agents (deepagents: LEARN mode, TEACH/REPAIR for pages and forms, scripted equivalents)

### Task W3-1: interface additions

**Files:** Modify `agents/interface.py` (V10 `start_learn`, `start_build(recording_id, name=None)`, `Mode` + `"learn"`,
`EVENT_TYPES` + `evidence`, `learned`), `agents/scripted.py` and `agents/deep.py` signatures; Test `tests/test_agent_learn_interface.py`.

- [ ] **Step 1: Failing tests:** `test_start_learn_thread_id_prefix` (`learn-`), `test_new_event_types_registered`,
  `test_start_build_accepts_name`; existing `tests/test_scripted_agent.py`, `tests/test_agent_reset.py` still pass.
- [ ] **Steps 2–5.**

### Task W3-2: deterministic learn helpers (no langchain at import)

**Files:** Create `agents/learn.py`; Test `tests/agents/test_learn_helpers.py`.

**Interfaces:** `page_outline(record: dict, max_chars: int = 4000) -> str` (title, h1–h3, og/twitter meta, JSON-LD keys and
short values, each with a css path from W0-1 helpers); `run_page_parser(parser: dict, record: dict, html_field="html", text_field="text") -> dict`
(fields or `{"error": …}`, via `runner.lib` helpers only); `evidence(record, fields: dict, validate: list[dict]) -> list[dict]`
(V10 `evidence.fields` items); `corroborated(record, value) -> bool`; `ambiguity(record, parser) -> dict[str, list[str]]`;
`additive_parser_patch(active_spec: dict, new_spec: dict) -> bool`.

- [ ] **Step 1: Failing tests** on `fixtures/pages`: `test_outline_is_small_and_has_candidates` (≤ 4000 chars, contains
  the expected title and company on all 8); `test_evidence_ok_for_spike_readers`; `test_fabricated_value_fails_evidence`
  (`company: "Acme Inc"` → `ok: False`); `test_page_chrome_is_not_corroborated` (a cookie-banner sentence from the Lever
  page's visible text → `False`; expected title/company → `True` on all 8); `test_ashby_h1_is_ambiguous`;
  `test_additive_patch_detection` (add parser → True; add parser + change `sink.url` → False).
- [ ] **Steps 2–5.**

### Task W3-3: LEARN tools and the D2 guardrail

**Files:** Modify `agents/deep_tools.py` (tools `get_page(page_id)`, `test_page_parser(parser)` = run + evidence +
corroboration + ambiguity in one call, `save_parser(parser, reason)` = add parser with `origin: learned` → validate →
compile → tests → D2 decision → deploy or `request_deploy`; emits `evidence` and `learned`); `test_parser` also handles
`css`/`jsonld`; `read_openapi`/`diff_openapi` go through `control.ops.fetch_contract`; Test `tests/agents/test_learn_tools.py`.

**Interfaces:** Consumes W1-1 models, `control.ops.compile_and_test/deploy`, `control.api.settings` learn_policy (read the
settings file via the existing settings helper; don't import FastAPI). Pages come from `control.pages.PageInbox` (W4a); until
merged, a `get_page_record(page_id) -> dict` seam in `ToolContext` that tests override.

- [ ] **Step 1: Failing tests** (temp workspace, `fixtures/jobs` compiled and deployed with the `greenhouse` parser
  removed, fixture form on a free port): `test_save_parser_auto_deploys_when_clean` (new version active, parser
  `origin.approved_by == "auto"`, `learned.approval == "auto"`); `test_save_parser_asks_when_ambiguous`
  (`request_deploy` emitted, not deployed); `test_save_parser_asks_when_not_corroborated`;
  `test_policy_approve_always_asks`; `test_evidence_failure_refuses_compile` (returns `REFUSED…`, no new version);
  `test_save_parser_rejects_non_additive` (a parser whose field set differs → validator error text returned).
- [ ] **Steps 2–5.**

### Task W3-4: LEARN mode on the deep agent

**Files:** Modify `agents/deep.py` (mode-specific supervisor tools and subagents: `learner` with `get_page`,
`test_page_parser`, `ask_user`, `save_parser`, `add_test_case`; `_run_learn`), `agents/prompts.py` (`LEARN_TASK`,
learner prompt); Test `tests/agents/test_deep_learn.py`, scripts in `tests/agents/scripts.py`.

- [ ] **Step 1: Failing test** with `ScriptedChatModel`: `test_deep_learn_branded_greenhouse` → event order
  `status(learn) … todos … subagent_start(learner) … tool_call get_page … test_page_parser … evidence … save_parser … learned(auto) … deployed … done`;
  `done.result.model_calls <= 8`; `test_learn_tools_per_agent` (supervisor has no write tools; learner can't call
  `request_deploy` directly); `test_learn_writes_only_spec` (filesystem writes limited to `/spec.yaml`).
- [ ] **Steps 2–5.**

### Task W3-5: TEACH for page → form, REPAIR for a renamed form question (deep)

**Files:** Modify `agents/deep_tools.py` (`draft_spec` from V11 candidates → V7/V5 spec with one `taught` parser;
`diff_openapi` pairs one removed + one added property of equal type as a rename and returns
`suggested_questions` only when the pairing isn't unique), `agents/prompts.py`; Test `tests/agents/test_deep_pages.py`.

- [ ] **Step 1: Failing tests:** `test_draft_spec_from_fixture_jobs` (validates; parser `lever` with the candidate's css;
  sink fields `Link: record.canonical_url`, `Date Applied: record.captured_date`, `Email: {const: demo@example.com}`); `test_diff_openapi_form_rename`
  (`Company → Employer` suggested, no question); `test_deep_repair_form_rename` (fake model: JSON Patch `move`
  `/sink/fields/Company` → `/sink/fields/Employer`, golden migrated, `request_deploy` emitted).
- [ ] **Steps 2–5.**

### Task W3-6: scripted LEARN, TEACH(jobs), REPAIR(form) — the stage fallback

**Files:** Modify `agents/scripted.py`; Test `tests/test_scripted_v2.py`.

- [ ] **Step 1: Failing tests:** `test_scripted_learn_emits_same_event_types_as_deep` (set of types equal to W3-4's
  run); `test_scripted_learn_uses_real_checks` (it calls the same `test_page_parser`/`save_parser` tool functions, so a
  fabricated value would be refused); `test_scripted_teach_fixture_jobs`; `test_scripted_form_repair`.
- [ ] **Steps 2–5.**

### Task W3-7: opt-in live LEARN and REPAIR

**Files:** Modify `tests/agents/test_deep_live.py` (`test_live_learn`, `test_live_form_repair`; `-m live`).

- [ ] **Step 1:** Run each once on `PATCHWORK_MODEL=particle-ai/particle-1:free`; record calls and seconds in
  `agents/README.md` (table, like v1's). Stay within the W3 budget (≤ 150 calls in total). **Step 2:** Commit.

---

## W4a. Control plane

### Task W4a-1: page inbox store

**Files:** Create `control/pages.py` (`PageInbox(path=None)`: `add(url, title, html) -> dict`, `list(since: int = 0, limit: int = 100) -> list[dict]`,
`get(page_id) -> dict | None`, `clear() -> int`; SQLite at `$PATCHWORK_WORKSPACE/pages.db`); Test `tests/control/test_pages_store.py`.

- [ ] **Step 1: Failing tests:** `test_add_builds_v1_record` (all V1 keys; `site`, `canonical_url`, `captured_date`
  local); `test_list_since_is_exclusive_and_ordered`; `test_clear`; `test_concurrent_adds_get_unique_seq` (10 threads).
- [ ] **Steps 2–5.**

### Task W4a-2: `/api/pages`, extension token, run on arrival

**Files:** Create `control/api/pages.py` (router); Modify `control/api/server.py` (include router, CORS for
`chrome-extension://`), `control/api/runs.py` (queue one follow-up run per integration; `PATCHWORK_CONTROL_URL` in the
runner env), `control/api/settings.py` (token file `$WS/extension_token`); Test `tests/control/test_api_pages.py`.

- [ ] **Step 1: Failing tests:** `test_post_requires_token` (401 without/with wrong token); `test_post_too_large` (413);
  `test_get_pages_since`; `test_post_starts_run_for_page_integrations` (fake compiled integration whose spec source is
  `patchwork://control`: a run starts; v1 integrations are not run); `test_presses_during_run_are_queued_once` (3 posts
  while a run is busy → exactly one follow-up run, all 3 pages processed); `test_same_posting_twice_is_duplicate`
  (end to end with W1 merged; until then `xfail(strict=False)` with reason "needs W1");
  `test_extension_setup_endpoint`.
- [ ] **Steps 2–5.**

### Task W4a-3: learn trigger, replay after deploy, tracker and layouts

**Files:** Create `control/tracker.py` (pure functions: `tracker_rows(store, inbox, agent, integration, limit) -> dict`,
`layouts(store, integration) -> list[dict]`), `control/layouts.py` (`revert_parser(name, parser_id) -> dict`: spec minus
parser → `compile_and_test` → `deploy`); Modify `control/api/pages.py` (`/api/tracker`, `/api/layouts`, revert),
`control/api/runs.py` (on finish: `PARSE_NO_MATCH` → `agent.start_learn` once per site; on agent `deployed` for a page
integration → replay then run); Test `tests/control/test_api_tracker.py`.

- [ ] **Step 1: Failing tests:** `test_unknown_layout_starts_one_learn_per_site` (fake agent records `start_learn`);
  `test_unknown_layout_with_ai_off_is_held` (no `start_learn`; row `status: held`, `reason: PARSE_NO_MATCH`);
  `test_deployed_event_replays_then_runs`; `test_labels` (code / learned_now / held / duplicate from store rows and a
  fake learn thread with `model_calls: 5` → `ai_calls: 5` only on that page); `test_layouts_from_manifests`;
  `test_revert_parser_compiles_and_deploys` and `test_revert_last_parser_409`.
- [ ] **Steps 2–5.**

### Task W4a-4: settings, build name, fixture-jobs, v2 demo controls, bench v2

**Files:** Modify `control/api/settings.py` (`learn_policy`), `control/api/server.py` (`BuildRequest.name`),
`control/api/recordings.py` (`fixture-jobs` → `fixtures/recording_jobs/`), `control/api/demo.py` (`/api/demo/form/rename`,
`/api/demo/v2/save-baseline`, `/api/demo/v2/reset`, `/api/bench/v2`); Test `tests/control/test_api_v2_demo.py`.

- [ ] **Step 1: Failing tests:** `test_learn_policy_roundtrip` (default `auto`; 422 on other values);
  `test_fixture_jobs_recording_listed`; `test_v2_reset_restores_baseline_and_keeps_v1` (po_to_stockroom versions, runs and
  processed keys untouched; jobs pages/records/processed/cursor cleared; active version = baseline);
  `test_form_rename_proxy`; `test_bench_v2_404_then_200`. Existing `tests/control` all pass.
- [ ] **Steps 2–5.**

---

## W4b. UI

### Task W4b-1: API types and mock server

**Files:** Modify `web/src/api.ts`, `web/mock/server.mjs`, `web/mock/data.json` (pages, tracker rows covering every
label, layouts, extension, bench v2, a learn thread with `evidence` and `learned` events); Test `web/src/api.test.ts`.

- [ ] **Step 1:** Failing vitest cases for the new client calls against the mock. **Steps 2–5.**

### Task W4b-2: Tracker tab

**Files:** Create `web/src/tabs/Tracker.tsx`, `web/src/lib/tracker.ts` (pure: `labelText(row)`, `totals(rows)`);
Modify `web/src/App.tsx` (Tracker first; header stays on one line at 1280 px, as in c4f606c); Test `web/src/lib/tracker.test.ts`.

- [ ] **Step 1: Failing tests:** `labelText` gives `code · 0 AI calls`, `learned now · 5 AI calls (checked by code)`,
  `held: unknown layout`, `held: form changed`, `already in tracker`, and `scripted` wording when `settings.agent === "scripted"`.
- [ ] **Step 2–4:** Polls `/api/tracker` every 1 s; each row shows site, title, company, parser + version, routed_by,
  label chip, AI calls; the counter "AI calls today"; the open `CONTRACT_CHANGED` incident banner with the existing Repair
  flow; evidence from the learn thread on expand (value, where found, ✓/✗). Extension setup card (control URL + token, copy buttons).
- [ ] **Step 5:** `npm test && npm run typecheck && npm run build`; commit.

### Task W4b-3: Learned layouts panel and approvals history

**Files:** Create `web/src/components/Layouts.tsx`; Test `web/src/lib/tracker.test.ts` (`layoutRows` ordering, origin wording).

- [ ] Steps: failing test → implement (site, parser, fingerprint, learned when, approved by auto/human, Revert button with
  confirm) → pass → commit.

### Task W4b-4: AI calls over postings chart, presenter mode

**Files:** Modify `web/src/tabs/Benchmark.tsx`, `web/src/lib/bench.ts`; Create `web/src/components/Presenter.tsx`
(`?present=1`: big tracker, counter, current beat of the 2-minute script, keyboard `→` to advance hints); Test `web/src/lib/bench.test.ts`.

- [ ] Load the `dataviz` skill before writing the chart. Failing test for the cumulative-series derivation with `kind`
  labels (`measured` solid, `estimate` dashed) → implement → `npm test && npm run typecheck && npm run build` → commit.

---

## W5. Proof + pitch

### Task W5-1: real-posting fixture set with an answer key

**Files:** Create `scripts/fetch_postings.py`, `fixtures/postings/<platform>/<n>.html.gz`, `fixtures/postings/answer_key.json`; Test `tests/bench/test_v2_postings.py`.

- [ ] **Step 1:** Fetch ~60 rendered postings across 4–5 platforms (Lever, Greenhouse hosted, Greenhouse branded,
  Ashby, one more public ATS), sequential, ≤ 1 req/s, desktop UA. **Step 2:** Answer key: title/company read by a person's
  method (JSON-LD/meta cross-checked with the visible heading; disagreements resolved by eye and listed in
  `answer_key.json` `notes`). **Step 3:** Test `test_answer_key_matches_files` (every file exists, gz decodes, title occurs
  in visible text). **Step 4:** Commit.

### Task W5-2: v2 benchmark harness

**Files:** Create `bench/v2/{__init__,run,scoring}.py`; Modify `Makefile` (`bench-v2`); Test `tests/bench/test_v2_scoring.py`.

**Interfaces:** Produces `bench/v2/results.json` (V12). Runs its own stack in-process on free ports (fixture form, control
plane, scripted agent), starting from a library that knows only the taught platform; posts postings in a fixed shuffled
order; records label, AI calls (learn events; per-learn calls from W3-7's measured deep runs, labelled
`measured (earlier run)`), seconds, correctness vs the answer key, and the browser-agent line (`estimate`, assumption written).

- [ ] **Step 1: Failing tests:** `test_cumulative_series`, `test_every_number_has_kind_and_source`,
  `test_wrong_row_counts_as_bad_write`. **Steps 2–5.**

### Task W5-3: `make smoke-v2` (the 2-minute script, automated)

**Files:** Create `scripts/smoke_v2.py`; Modify `Makefile`; Test `tests/bench/test_v2_smoke_offline.py` (runs the smoke
against an in-process stack with the fixture form and the scripted agent).

- [ ] **Step 1:** Beats, each asserted: v2 reset to baseline → 3 known postings → `code`, 0 AI calls → unknown platform →
  `learned_now` → second posting of it → `code`, 0 AI calls → rename "Company"→"Employer" (fixture form: API; Google
  Form: print the exact instruction and wait for Enter) → next posting `held` before any write → repair → approve → row
  written → rows confirmed (fixture form `/admin/rows`; Google: Sheet CSV row delta). Prints a timing per beat; exit 0 iff all pass.
- [ ] **Step 2:** Offline test passes. **Step 3:** Commit.

### Task W5-4: demo script and backup checklist

**Files:** Create `docs/demo-v2.md` (the 2-minute script with exact clicks and fallback lines, rehearsal reset, what's
measured vs estimated, backup video checklist, the "how is this different" slide text from design §1).

- [ ] Commit.

---

## Integration (integrator)

### Task I-1: merge and verify each workstream

- [ ] For each branch in merge order: `git merge --no-ff <branch>`; `.venv/bin/python -m pytest -q`; `cd web && npm test && npm run typecheck && npm run build`;
  fix breaks on the integration branch; commit the fix with the attribution line. Re-run the v1 `make smoke` on free ports
  (own workspace) after W1 and W4a merges.

### Task I-2: real-form bootstrap

- [ ] Teach `jobs_to_tracker` against the demo Google Form (owner's live recording, or `fixture-jobs` retargeted to the
  demo Form URL and said so), learn Lever, Greenhouse and Ashby off camera (C8), `save-baseline`.

### Task I-3: definition of done (design §9)

- [ ] The 2-minute script runs end to end three times in a row on the deep agent and once on the scripted agent
  (`make smoke-v2` against the demo Form + Sheet); all v1 and v2 tests pass; benchmark numbers labelled. Then update
  `README.md` and `ROADMAP.md`.

### Task I-4: owner steps (batched in one message)

- [ ] Load the unpacked extension and set the shortcut; record the teaching once (if live); press the hotkey on live pages
  for the rehearsal; rename the Form question for the break beat; clear SPIKE/rehearsal rows from the demo Sheet.
