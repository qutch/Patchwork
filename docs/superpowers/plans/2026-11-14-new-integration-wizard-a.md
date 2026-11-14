# New Integration Wizard (Approach A) and Integrations Home: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Anyone can add an integration for any subject from one "New integration" button. They describe it, pick
a destination and credential, send example pages, then review and approve. Everything about an integration lives
on one page.

**Architecture:** A wizard creates a *draft integration* on the server. Example pages arrive through the existing
page inbox, tagged with the draft's name. "Teach" writes a synthetic recording (`draft-<name>`) and starts the
existing BUILD on it:
- **Candidates:** the destination contract plus the example pages, with no mappings.
- **Agent:** TEACH in the one deep agent. On these candidates it runs a *from examples* variant with two new tools:
  - `propose_mapping` maps destination fields to integration fields.
  - `add_site_parser` adds one parser per example site.

  Both tools check everything in code. The scripted agent mirrors it.
- **Approve:** the person approves a review table, and their edits are applied by code. Compile, test and deploy
  are today's; the example pages then run.
- **UI:** the web UI becomes Home → Integration page → sections, wrapping today's tab components.

**Tech Stack:** Python 3.12, FastAPI, deepagents 0.7.38 (`agents/deep.py`), selectolax, Playwright (forms), React +
TypeScript + Vite + Vitest (`web/`), the node mock server (`web/mock`).

**Spec:** `docs/superpowers/specs/2026-11-14-new-integration-flow-design.md` (approved 2026-11-14). Read it with this
plan. `docs/contracts.md` V4–V16 define every existing shape mentioned here.

## Global Constraints

- **Agents:**
  - All AI work goes through `agents/deep.py` `DeepAgentService` (deepagents==0.7.38). TEACH, LEARN and REPAIR are
    modes of the one agent. TEACH-from-examples is a TEACH variant, not a new agent.
  - Guardrails live in tools (`agents/deep_tools.py`, checks in `agents/learn.py`), never only in prompts.
  - `ScriptedAgentService` gets a matching script that emits the same events.
- **Runner:** stays AI-free (subprocess, stripped env, no model keys, refuses to start with AI modules loaded).
  Credentials reach it only as `PATCHWORK_CRED__<NAME>` via `control.ops.runner_env(spec)`.
- **Records:** never silently write a wrong row; when in doubt, hold the record for a person. Every AI-produced
  value is checked by code and labelled in the UI.
- **v1:** supplier POs → Stockroom (`po_to_stockroom`) keeps working unchanged. All existing tests stay green.
  `scripts/smoke_v2.py --in-process`, `--sink airtable --in-process` and the v1 bench (2/2 breaks caught) still
  pass.
- **Ports:** never bind, kill or reset 8000–8003, 5180 or 38091. Tests use free ports, in-process servers and a
  separate `PATCHWORK_WORKSPACE`.
- **Secrets:** never print, log or commit `.env` values. Credential values never appear in an API response, event,
  log or the UI.
- **Model budget:** prefer the scripted agent and offline fake models in tests. A live TEACH from examples is at
  most 10 model calls and about 15 s on `anthropic:claude-haiku-5-5`.
- **No job words:** no job-specific wording or assumptions anywhere in the new flow (the design's "any use case"
  goal). The tuned job learner prompt stays only where `agents.prompts.is_job_integration` is true.
- **Git:** commit on `project-roadmap-mvp-68d423de` (or merge worktree branches into it). Every commit message ends
  with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Example pages from two sites with different markup:** product title in `h1` on one site and `h2.name` on the
   other. Expect one parser per site, never a reader shared across sites (tested in Task 5).
2. **A required destination field nothing on the page explains:** expect an `ask_user` with "Skip this field" or a
   constant. If the person skips it, Approve is refused with "<field> is required by the destination", not a
   write that fails later (tested in Task 5).
3. **A credential added in the UI, then used straight away without a restart:** its value is redacted from every
   later response and event at once (tested in Task 1).
4. **The person reloads the wizard mid-way, or a second tab opens it:** the step state comes back from the
   server, and Teach can't start twice for one draft (409) (tested in Task 2).
5. **A page sent to a draft before its destination is set:** it waits in the inbox. It never triggers a run or a
   LEARN, and it is offered as an example in step 3 (tested in Task 2).

---

### Task 1: Credentials can be added from the UI

**Files:**
- Modify: `control/credentials.py`, `control/api/credentials.py`, `runner/credentials.py`
- Test: `tests/control/test_api_credentials.py` (extend)

**Interfaces:**
- Produces:
  - `control.credentials.set_credential(name: str, value: str, env_path: Path | None = None) -> None`.
  - `POST /api/credentials {name, value}` → 201 `{name, set: true, used_by: [...]}`.
  - `runner.credentials.redact` also blanks values of every set name listed by
    `control.credentials.known_names()`.

- [ ] **Step 1: Write the failing tests**

```python
def test_post_credential_sets_env_and_never_echoes(client, tmp_path, monkeypatch):
    env = tmp_path / ".env"; env.write_text("OTHER=1\n")
    monkeypatch.setattr("control.credentials.ENV_PATH", env)
    r = client.post("/api/credentials", json={"name": "SHOP_TOKEN", "value": "sentinel-cred-123456"})
    assert r.status_code == 201 and "sentinel-cred-123456" not in r.text
    assert os.environ["SHOP_TOKEN"] == "sentinel-cred-123456"
    assert "SHOP_TOKEN=sentinel-cred-123456" in env.read_text() and "OTHER=1" in env.read_text()
    assert oct(env.stat().st_mode & 0o777) == "0o600"
    assert {"name": "SHOP_TOKEN", "set": True} .items() <= next(c for c in client.get("/api/credentials").json() if c["name"] == "SHOP_TOKEN").items()

def test_post_credential_refuses_model_and_process_names(client):
    for bad in ("ANTHROPIC_API_KEY", "PATH", "lower"):
        assert client.post("/api/credentials", json={"name": bad, "value": "x" * 12}).status_code == 422

def test_new_credential_is_redacted_immediately(client, monkeypatch, tmp_path):
    monkeypatch.setattr("control.credentials.ENV_PATH", tmp_path / ".env")
    client.post("/api/credentials", json={"name": "SHOP_TOKEN", "value": "sentinel-cred-123456"})
    from runner.credentials import redact
    assert redact("x sentinel-cred-123456 y") == "x *** y"

def test_replacing_a_credential_rewrites_its_line(client, monkeypatch, tmp_path):
    env = tmp_path / ".env"; env.write_text("SHOP_TOKEN=old-value-0001\n"); monkeypatch.setattr("control.credentials.ENV_PATH", env)
    client.post("/api/credentials", json={"name": "SHOP_TOKEN", "value": "new-value-0002"})
    assert env.read_text().count("SHOP_TOKEN=") == 1 and "old-value-0001" not in env.read_text()
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `.venv/bin/python -m pytest tests/control/test_api_credentials.py -q`
Expected: the 4 new tests FAIL (405 or 404 on POST).

- [ ] **Step 3: Implement**
  - `set_credential` checks the name with `runner.credentials.check_name` (`ValueError` → 422).
  - It rewrites or appends one `NAME=value` line in `ENV_PATH` (default: repo `.env`) atomically: a temp file,
    `os.replace`, then `chmod 0o600`.
  - It sets `os.environ[name]`.
  - `known_names()` returns the names from `.env` plus the names used by specs.
  - `redact()` takes them through its existing `names=` parameter. Call it from the API redaction middleware with
    `names=known_names()`.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/control/test_api_credentials.py tests/test_credentials.py -q`
Expected: PASS.

- [ ] **Step 5: Commit** with message `Credentials: add from the UI (POST /api/credentials), redacted at once`.

### Task 2: Draft integrations and full delete

**Files:**
- Create: `control/drafts.py`, `control/api/drafts.py`
- Modify:
  - `control/api/server.py`: include the router; `/api/integrations` lists drafts; `/api/extension` lists drafts.
  - `control/api/pages.py`: accept a draft's name.
  - `control/page_hooks.py` and `control/autorun.py`: never run or learn for a draft.
- Test: `tests/control/test_drafts.py`

**Interfaces:**
- Produces:
  - `control.drafts.Draft` (pydantic): `name, intent, state: Literal["draft","teaching","live"],
    destination: dict | None, example_page_ids: list[str], thread_id: str | None, created_at`.
  - `control.drafts.create(name: str, intent: str) -> Draft`.
  - `control.drafts.get(name) -> Draft | None`, `control.drafts.list_all() -> list[Draft]`,
    `control.drafts.update(name, **fields) -> Draft`.
  - `control.drafts.delete_integration(name: str, *, delete_rows: bool) -> dict`. It removes the draft, versions,
    pages, learn links and threads. When `delete_rows` is true it deletes only the destination rows whose record
    ids Patchwork stored.
  - Drafts are stored as `$PATCHWORK_WORKSPACE/drafts/<name>.json`.
  - API:
    - `POST /api/integrations/new {name?, intent}` → 201 Draft (name = `agents.deep_tools.intent_slug(intent)`
      when omitted; 409 if taken).
    - `GET /api/integrations/{name}/draft`.
    - `DELETE /api/integrations/{name}?delete_rows=false`.
  - `GET /api/integrations` gains `state` per entry (`draft|teaching|live`).
  - `GET /api/extension` `integrations[]` includes drafts.

- [ ] **Step 1: Write the failing tests**

```python
def test_create_lists_draft_everywhere(client):
    d = client.post("/api/integrations/new", json={"intent": "Track upcoming events"}).json()
    assert d["name"] == "track_upcoming_events" and d["state"] == "draft"
    assert any(i["name"] == d["name"] and i["state"] == "draft" for i in client.get("/api/integrations").json())
    assert d["name"] in [i["name"] for i in client.get("/api/extension", headers=TOKEN).json()["integrations"]]

def test_page_to_draft_waits_without_run_or_learn(client, page_runner_spy, learn_spy):
    client.post("/api/integrations/new", json={"intent": "Track upcoming events"})
    r = client.post("/api/pages", json={"url": "https://ev.example.com/e/1", "title": "t", "html": "<h1>x</h1>",
                                        "integration": "track_upcoming_events"}, headers=TOKEN)
    assert r.status_code == 201 and r.json()["queued"] is False
    assert page_runner_spy.calls == [] and learn_spy.calls == []

def test_duplicate_name_is_409(client):
    client.post("/api/integrations/new", json={"intent": "Track upcoming events"})
    assert client.post("/api/integrations/new", json={"intent": "Track upcoming events"}).status_code == 409

def test_delete_removes_everything_but_credentials(client, built_products_integration):
    r = client.delete(f"/api/integrations/{built_products_integration}?delete_rows=false")
    assert r.status_code == 200
    assert built_products_integration not in [i["name"] for i in client.get("/api/integrations").json()]
    assert client.get(f"/api/pages?integration={built_products_integration}").json() == []
```

- [ ] **Step 2: Run them and confirm they fail.** Run `.venv/bin/python -m pytest tests/control/test_drafts.py -q`.
  Expected: FAIL (404 on `/api/integrations/new`).

- [ ] **Step 3: Implement.** A draft name also counts as a page integration in `control/api/pages.py`. `queued` is
  false and no run starts while `state != "live"`. `page_hooks.maybe_learn` and the autorun skip non-live names.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/control -q`
Expected: PASS, including the existing `test_api_pages.py` and `test_pages_routing.py`.

- [ ] **Step 5: Commit** with message `Drafts: create, list, tag pages, delete an integration completely`.

### Task 3: Choose a destination and check the connection (Airtable, form, API, SQLite)

**Files:**
- Create: `control/destinations.py`
- Modify:
  - `control/api/drafts.py`
  - `control/api/server.py`: `Destination` becomes a discriminated union by `kind`.
- Test: `tests/control/test_destinations.py`

**Interfaces:**
- Consumes:
  - `control.ops.fetch_contract(spec)` (A1/A2).
  - `control.ops.fetch_sqlite_contract(path, table)`, which exists only if PR #2 is merged. Otherwise leave
    `sqlite` out of the union and its test skipped.
- Produces:
  - `control.destinations.check(dest: dict) -> dict` returns `{kind, title, fields: [{name, type, required,
    options?}], operations?: [...]}`. It raises `DestinationError(message)` with a next-step message: the
    credential was rejected, the table wasn't found, or the host couldn't be reached.
  - `control.destinations.airtable_tables(base_id, credential) -> list[{id, name}]`.
  - `control.destinations.api_operations(base_url, credential=None) -> list[{method, path, summary,
    fields: [dotted paths]}]`. It lists POST, PUT and PATCH operations only. Body schemas are flattened to dotted
    paths with `required` from the schema.
  - API:
    - `PUT /api/integrations/{name}/destination {kind, ...}` → the check result, saved on the draft.
    - `GET /api/destinations/airtable/tables?base_id=&credential=`.
    - `GET /api/destinations/api/operations?base_url=&credential=`.
  - The destination dict per kind:
    - airtable: `{kind:"airtable", base_id, table, credential}`; a base URL is accepted and parsed with
      `base_id_from_url`.
    - form: `{kind:"form", url}`.
    - api: `{kind:"api", base_url, operation: {method, path}, credential?}`.
    - sqlite: `{kind:"sqlite", path, table}`.

- [ ] **Step 1: Write the failing tests** against fake Airtable (`demo_apps/fake_airtable`), the fixture tracker form,
  and a fake papers API (Task 9 creates it; until then use a minimal FastAPI app defined in the test):

```python
def test_airtable_url_parsed_and_fields_typed(fake_airtable, client, draft):
    r = client.put(f"/api/integrations/{draft}/destination", json={"kind": "airtable",
        "base_url": "https://airtable.com/appTEST00000000001/shrX", "table": "Products", "credential": "AIRTABLE_TOKEN"})
    assert r.status_code == 200 and {"name": "Price", "type": "number"}.items() <= r.json()["fields"][1].items()

def test_rejected_credential_says_what_to_do(fake_airtable_rejecting, client, draft):
    r = client.put(f"/api/integrations/{draft}/destination", json={"kind": "airtable", "base_id": "appTEST00000000001",
        "table": "Products", "credential": "AIRTABLE_TOKEN"})
    assert r.status_code == 422 and "rejected the credential AIRTABLE_TOKEN" in r.json()["detail"]

def test_api_operations_flatten_nested_body(papers_api, client):
    ops = client.get(f"/api/destinations/api/operations?base_url={papers_api}").json()
    post = next(o for o in ops if o["method"] == "POST")
    assert "authors.0.name" not in post["fields"] and {"title", "venue.name", "year"} <= set(post["fields"])

def test_get_operations_are_not_offered(papers_api, client):
    assert all(o["method"] in ("POST", "PUT", "PATCH") for o in client.get(f"/api/destinations/api/operations?base_url={papers_api}").json())
```

- [ ] **Step 2: Run them and confirm they fail.** Run `.venv/bin/python -m pytest tests/control/test_destinations.py -q`.
  Expected: FAIL.
- [ ] **Step 3: Implement**
  - Arrays in body schemas are not flattened. A field under an array is listed once as `<path>[]` and left
    unmapped.
  - The wizard maps scalar leaves only.
- [ ] **Step 4: Run the tests and confirm they pass.** Run `.venv/bin/python -m pytest tests/control -q`. Expected:
  PASS.
- [ ] **Step 5: Commit** with message `Destinations: check any destination kind, list Airtable tables and API write
  operations`.

### Task 4: Teach inputs from a draft (a synthetic recording)

**Files:**
- Modify:
  - `control/drafts.py`
  - `control/api/drafts.py`
  - `control/api/recordings.py`: `is_page_form` is true for `draft-*` recordings.
- Test: `tests/control/test_draft_teach.py`

**Interfaces:**
- Consumes: `control.recordings.recording_dir`, `PageInbox.get`, `control.destinations.check`.
- Produces:
  - `control.drafts.write_teach_inputs(name: str, page_ids: list[str]) -> str`. It returns the recording id
    `draft-<name>` and writes:
    - `recording.json`: `{id, intent, events: [], start_url}`.
    - `snapshots/<n>.html`.
    - `candidates.json`: `{recording_id, intent, teach_mode: "examples", source: {kind:"page", ...first page},
      examples: [{page_id, site, url, snapshot}], sink: <destination + contract fields as labels/questions>,
      mappings: [], unexplained: []}`.
  - `POST /api/integrations/{name}/teach {page_ids}`:
    - 409 if `state != "draft"`; 422 if there is no destination or no example page.
    - Otherwise it calls the same code as `POST /api/integrations/build`, with `recording_id`, `name` and the
      draft destination. It sets `state="teaching"` and `thread_id`, and returns `{thread_id}`.

- [ ] **Step 1: Write the failing tests**

```python
def test_teach_writes_examples_candidates(client, products_draft_with_pages, fake_agent):
    name, pids = products_draft_with_pages
    tid = client.post(f"/api/integrations/{name}/teach", json={"page_ids": pids}).json()["thread_id"]
    c = json.loads((recording_dir(f"draft-{name}") / "candidates.json").read_text())
    assert c["teach_mode"] == "examples" and c["mappings"] == [] and len(c["examples"]) == len(pids)
    assert [f["label"] for f in c["sink"]["questions"]] == ["Product", "Price", "Store", "Link", "Date Added"]
    assert fake_agent.builds == [(f"draft-{name}", name)] and tid

def test_teach_twice_is_409(client, products_draft_with_pages, fake_agent):
    name, pids = products_draft_with_pages
    client.post(f"/api/integrations/{name}/teach", json={"page_ids": pids})
    assert client.post(f"/api/integrations/{name}/teach", json={"page_ids": pids}).status_code == 409

def test_teach_without_destination_is_422(client, draft_with_pages_no_destination):
    name, pids = draft_with_pages_no_destination
    assert client.post(f"/api/integrations/{name}/teach", json={"page_ids": pids}).status_code == 422
```

- [ ] **Step 2: Run them and confirm they fail.** Run `.venv/bin/python -m pytest tests/control/test_draft_teach.py -q`.
  Expected: FAIL.
- [ ] **Step 3: Implement.** The deploy hook sets the draft's state to `live` (via `control.ops.deploy` or the build
  thread's end). It then starts one run of the integration, so the example pages are written.
- [ ] **Step 4: Run the tests and confirm they pass.** Run `.venv/bin/python -m pytest tests/control -q`. Expected:
  PASS.
- [ ] **Step 5: Commit** with message `Drafts: teach from example pages through the existing build`.

### Task 5: The tools for teaching from examples (`propose_mapping`, `add_site_parser`, review edits)

**Files:**
- Modify:
  - `agents/deep_tools.py`: the tools, `request_deploy` review payload and edits.
  - `agents/learn.py`: `guess_reader`.
  - `agents/interface.py`: `request_deploy` event fields.
  - `control/api/server.py`: `ResumeRequest.edits`.
- Test: `tests/agents/test_teach_examples_tools.py`

**Interfaces:**
- Consumes: `agents.learn.evidence`, `run_page_parser`, `literal_capture`; `deep_tools.draft_page_spec`;
  `ToolContext.get_page_record`.
- Produces (tools, available to TEACH only when `candidates.teach_mode == "examples"`):
  - **`propose_mapping(mapping: dict[str, dict]) -> str`**
    - Keys are destination field names. Each value is one of:
      - `{"field": "<integration field>", "reader": {css|jsonld...}}`
      - `{"record": "canonical_url"}`
      - `{"today": true}`
      - `{"const": "..."}`
      - `{"skip": true}`
    - It checks every reader on the first example page with `evidence` (the value is on the page and passes the
      destination type) and `literal_capture`.
    - It writes `/spec.yaml` via `draft_page_spec`. The parser is for the first example's site; integration
      fields are named by `field`.
    - It returns `OK <summary>`, or `REFUSED: <field>: <reason>` per failing field.
    - A `const` on a field the person hasn't confirmed is recorded as an assumption, which the review shows as
      "needs you".
    - A required destination field set to `skip` → `REFUSED: <field> is required by the destination`.
  - **`add_site_parser(page_id: str, parser: dict) -> str`.** For another example site, it runs the same checks
    as `save_parser` (`_check`, evidence, ambiguity, literal capture) and appends the parser to `/spec.yaml`. It
    never deploys. The match is that page's exact site, or a platform wildcard under the existing at-least-half
    rule. A reader that doesn't fit gets `REFUSED`, and a shared catch-all is never allowed.
  - **`request_deploy(version, summary)`** also emits `review: [{dest_field, source, values: {page_id: value},
    ai_proposed: bool, status: "checked"|"needs_you", alternatives: [source...]}]`.
  - **Resume:**
    - The payload may hold `edits: [{dest_field, choice: <one of that row's alternatives> | {"const": str} |
      {"skip": true}}]`.
    - Edits are applied in code by `apply_review_edits(spec: dict, review: list, edits: list) -> dict`, which
      refuses any choice not listed in `alternatives`.
    - Then it recompiles via the existing `compile_spec` path and deploys. No model call is made.
- `agents.learn.guess_reader(dest_field: str, dest_type: str, page: dict) -> dict | None`: a reader from JSON-LD
  keys, `og:`/`twitter:` meta and `h1`, matched by name similarity (Product/Name/Title → `name`/`og:title`/`h1`;
  Price → `offers.price`). It is used by the scripted agent and to fill `alternatives`.

- [ ] **Step 1: Write the failing tests** using `fixtures/products` (shop-a, shop-b) with the fake Airtable contract:

```python
def test_mapping_reader_checked_on_first_page(teach_ctx):
    out = teach_ctx.call("propose_mapping", mapping={
        "Product": {"field": "name", "reader": {"css": "h1.product-title"}},
        "Price": {"field": "price", "reader": {"jsonld": "offers.price", "jsonld_type": "Product"}},
        "Link": {"record": "canonical_url"}, "Date Added": {"today": True}, "Store": {"skip": True}})
    assert out.startswith("OK") and teach_ctx.spec()["extract"]["parsers"][0]["match"] == {"site": "shop-a.example.com"}

def test_reader_not_on_page_is_refused(teach_ctx):
    out = teach_ctx.call("propose_mapping", mapping={"Product": {"field": "name", "reader": {"css": "h1.nope"}}})
    assert out.startswith("REFUSED") and "Product" in out

def test_second_site_gets_its_own_parser_never_catch_all(teach_ctx_mapped, shop_b_pid):
    assert teach_ctx_mapped.call("add_site_parser", page_id=shop_b_pid,
                                 parser={"fields": {"name": {"css": "h1.product-title"}}}).startswith("REFUSED")
    assert teach_ctx_mapped.call("add_site_parser", page_id=shop_b_pid,
                                 parser={"match": {}, "fields": {"name": {"css": "h2.name"}}}).startswith("REFUSED")
    out = teach_ctx_mapped.call("add_site_parser", page_id=shop_b_pid, parser={"fields": {"name": {"css": "h2.name"},
                                "price": {"jsonld": "offers.price", "jsonld_type": "Product"}}})
    assert out.startswith("OK") and [p["match"] for p in teach_ctx_mapped.spec()["extract"]["parsers"]][1] == {"site": "lumen-supply.example.org"}

def test_required_field_skipped_is_refused(teach_ctx_required_store):
    out = teach_ctx_required_store.call("propose_mapping", mapping={"Store": {"skip": True}})
    assert "Store is required by the destination" in out

def test_review_edit_must_be_an_offered_alternative(review, spec):
    with pytest.raises(ValueError):
        apply_review_edits(spec, review, [{"dest_field": "Product", "choice": {"css": "body"}}])
    assert apply_review_edits(spec, review, [{"dest_field": "Store", "choice": {"const": "Acme"}}])["sink"]["fields"]["Store"] == {"const": "Acme"}

def test_guess_reader_finds_product_price(shop_a_page):
    assert guess_reader("Price", "number", shop_a_page) == {"jsonld": "offers.price", "jsonld_type": "Product"}
```

- [ ] **Step 2: Run them and confirm they fail.** Run `.venv/bin/python -m pytest tests/agents/test_teach_examples_tools.py -q`.
  Expected: FAIL.
- [ ] **Step 3: Implement.** Reuse the `save_parser` check code by extracting it into a helper that both tools
  call; don't copy it. Tool results go through the existing redaction wrapper in `tool_functions`.
- [ ] **Step 4: Run the tests and confirm they pass.** Run
  `.venv/bin/python -m pytest tests/agents -q`. Expected: PASS, including the existing learn-tool tests.
- [ ] **Step 5: Commit** with message `TEACH from examples: propose_mapping and add_site_parser, checked in code; review edits applied by code`.

### Task 6: The deep agent's TEACH-from-examples mode

**Files:**
- Modify:
  - `agents/deep.py`: `_run_build` picks the examples variant when `candidates.teach_mode == "examples"`; tool
    lists per subagent.
  - `agents/prompts.py`: `TEACH_EXAMPLES_TASK` and the `mapper` subagent description, both domain-free.
- Test:
  - `tests/agents/test_deep_teach_examples.py`, with an offline fake chat model scripted like the other deep-agent
    tests.
  - `tests/agents/test_no_job_words.py`.

**Interfaces:**
- Consumes: Task 5 tools.
- Produces:
  - **Subagents:**
    - `mapper`: `get_candidates`, `get_page`, `test_page_parser`, `propose_mapping`, `ask_user`.
    - `learner`: `get_page`, `test_page_parser`, `add_site_parser` (no `save_parser` in this mode).
    - `builder`: `compile_spec`, `run_tests`.
  - **Supervisor:** `request_deploy`.
  - **Todo list (`write_todos`):** map fields → add a parser per other site → compile → request deploy.
  - **Budget:** at most 10 model calls with 2 example sites. `EventMiddleware` counts them.

- [ ] **Step 1: Write the failing tests**

```python
def test_examples_teach_runs_mapper_learner_builder(fake_model_service, products_draft_teach):
    ev = run_to_interrupt(fake_model_service, products_draft_teach)
    names = [e["name"] for e in ev if e["type"] == "subagent_start"]
    assert names[:1] == ["mapper"] and "learner" in names and "builder" in names
    deploy = next(e for e in ev if e["type"] == "request_deploy")
    assert {r["dest_field"] for r in deploy["review"]} == {"Product", "Price", "Store", "Link", "Date Added"}
    assert sum(1 for e in ev if e["type"] == "model_call") <= 10

PROMPTS = ["TEACH_EXAMPLES_TASK", "GENERIC_LEARNER"]
JOB_WORDS = re.compile(r"\b(job|posting|employer|company|hiring|applicant|resume)\b", re.I)
def test_new_flow_text_has_no_job_words():
    from agents import prompts, deep_tools
    for n in PROMPTS: assert not JOB_WORDS.search(getattr(prompts, n)), n
    for t in ("propose_mapping", "add_site_parser"): assert not JOB_WORDS.search(deep_tools.TOOL_DOCS[t])
```

- [ ] **Step 2: Run them and confirm they fail.** Expected: FAIL.
- [ ] **Step 3: Implement.** `TOOL_DOCS` is the dict of tool descriptions the tools are registered with. Add it if
  descriptions are inline today.
- [ ] **Step 4: Run the tests and confirm they pass.** Run
  `.venv/bin/python -m pytest tests/agents -q`. Expected: PASS.
- [ ] **Step 5: Commit** with message `Deep agent: TEACH from examples (mapper, learner per site, builder), domain-free`.

### Task 7: The scripted agent's TEACH from examples

**Files:**
- Modify: `agents/scripted_v2.py` (`_build_from_examples`, chosen from `_build_pages` when `teach_mode == "examples"`)
- Test: `tests/agents/test_scripted_teach_examples.py`

**Interfaces:**
- Consumes: Task 5 tools; `agents.learn.guess_reader`.
- Produces: the same event sequence as Task 6. Subagents are `mapper`, then `learner` once per other site, then
  `builder`, then `request_deploy` with `review`. A destination field `guess_reader` can't place becomes an
  `ask_user` whose options are the alternatives plus "Skip this field".

- [ ] **Step 1: Write the failing test.** The test runs a scripted build on the products draft (shop-a, shop-b →
  fake Airtable). It resumes with approve and asserts:
  - version 1 is live;
  - the parsers match `shop-a.example.com` and `lumen-supply.example.org`;
  - fake Airtable has 2 rows with `Price == 24.99` and `89.0`;
  - the run summary shows `ai_calls == 0`.
- [ ] **Step 2: Run it and confirm it fails.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run it and confirm it passes.** Run `.venv/bin/python -m pytest tests/agents tests/airtable -q`.
  Expected: PASS.
- [ ] **Step 5: Commit** with message `Scripted agent: TEACH from examples with the same events`.

### Task 8: An API destination with nested bodies

**Files:**
- Modify: `agents/deep_tools.py` (`draft_page_spec` for `sink.kind == "api"` with an `operation`)
- Test: `tests/agents/test_api_destination_teach.py`

**Interfaces:**
- Produces: `agents.deep_tools.dotted_to_body(fields: dict[str, dict]) -> dict` turns
  `{"venue.name": {"from": "venue"}}` into `{"venue": {"name": {"from": "venue"}}}`. `draft_page_spec` writes an
  `ApiSink` with `call` from the chosen operation and `body` from `dotted_to_body`. REPAIR uses the existing
  OpenAPI-diff path unchanged.

- [ ] **Step 1: Write the failing tests:**
  - `dotted_to_body` round-trips a 2-level mapping.
  - A scripted examples-TEACH on the papers fixture into the fake papers API (Task 9) writes one record whose
    JSON has `venue.name` nested.
- [ ] **Step 2: Run them and confirm they fail.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests and confirm they pass.** Run
  `.venv/bin/python -m pytest tests/agents tests/engine -q`. Expected: PASS.
- [ ] **Step 5: Commit** with message `API destinations in the wizard: one operation, nested body from dotted fields`.

### Task 9: Three domains, one flow (fixtures, fake API, smoke)

**Files:**
- Create:
  - `fixtures/events/` (two synthetic event pages from two sites, JSON-LD `Event`, plus `FORM_QUESTIONS` for
    `demo_apps.tracker_form`).
  - `fixtures/papers/` (two synthetic paper pages with `ScholarlyArticle` meta).
  - `demo_apps/fake_papers_api/` (FastAPI: `POST /papers` with a nested `venue.name`, `GET /papers`, OpenAPI at
    `/openapi.json`).
- Modify: `scripts/smoke_v2.py` (`--new-integration {products,events,papers}`), `Makefile`
  (`smoke-new-integration-offline`)
- Test: `tests/control/test_smoke_new_integration.py` (all three domains, scripted agent, in-process)

**Interfaces:**
- Produces: `smoke_v2.py --new-integration <domain> --in-process` walks the wizard API:
  1. new
  2. destination
  3. post 2 pages
  4. teach
  5. approve
  6. rows confirmed
  7. a third page from a new site is learned by LEARN
  8. rename a destination field → held → repair → written

  It prints numbered `ok` lines like today.

- [ ] **Step 1: Write the failing test.** Run the smoke for each domain and assert every beat is `ok`.
- [ ] **Step 2: Run it and confirm it fails.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run it and confirm it passes.** Run `.venv/bin/python -m pytest tests/control/test_smoke_new_integration.py -q`
  and `make smoke-new-integration-offline`. Expected: PASS, 3 domains.
- [ ] **Step 5: Commit** with message `Three-domain smoke: products to Airtable, events to a form, papers to an API`.

### Task 10: Web: hash routes, Home, Integration page

**Files:**
- Create:
  - `web/src/routes.ts`
  - `web/src/pages/Home.tsx`
  - `web/src/pages/IntegrationPage.tsx`
  - `web/src/routes.test.ts`
  - `web/src/pages/Home.test.tsx`
- Modify: `web/src/App.tsx`, `web/src/state.tsx` (route state replaces `tab`), `web/src/api.ts` (`IntegrationSummary.state`)

**Interfaces:**
- Produces:
  - `parseHash(h: string): Route` and `toHash(r: Route): string`, where
    `Route = {page:"home"} | {page:"new"} | {page:"integration", name: string, section: Section}` and
    `Section = "rows"|"needs"|"layouts"|"versions"|"activity"|"setup"`.
  - Legacy redirects:

    | Old hash | New route |
    |---|---|
    | `#tracker` | jobs `rows` |
    | `#agent` | the current integration's `activity` |
    | `#review` | `po_to_stockroom` `setup` |
    | `#integration` | `po_to_stockroom` `versions` |
    | `#runs` | `po_to_stockroom` `rows` |
    | `#bench` | `{page:"bench"}` |
    | `#record` | `{page:"new", mode:"show"}`, which Plan B fills in; until then the old Record view |

  - **Section components:**
    - `rows`: `Tracker` for page integrations, `Runs` for API→API.
    - `layouts`: `Layouts`.
    - `versions`: `Integration`.
    - `activity`: `Agent`, filtered by integration.
    - `needs`: incidents, held records and open interrupts for this integration.
    - `setup`: `Review` for v1.
  - Presenter mode stays and takes `?integration=`.

- [ ] **Step 1: Write the failing tests.**
  - `parseHash`/`toHash` round-trip every route, and each legacy hash redirects as listed.
  - Home renders one card per integration with state badges (Draft/Live), using the mock.
  - Clicking a card navigates to `#/integrations/<name>/rows`.
- [ ] **Step 2: Run them and confirm they fail.** Run `cd web && npx vitest run src/routes.test.ts src/pages`.
  Expected: FAIL.
- [ ] **Step 3: Implement.** Benchmark moves into the header's secondary menu.
- [ ] **Step 4: Run the checks and confirm they pass.** Run `cd web && npm run -s typecheck && npx vitest run && npm run -s build`.
  Expected: PASS, with the existing 91 tests still green.
- [ ] **Step 5: Commit** with message `Web: Integrations home and one page per integration; old tabs redirect`.

### Task 11: Web: the New integration wizard and the Credentials drawer

**Files:**
- Create:
  - `web/src/pages/NewIntegration.tsx` (steps What / Where / Examples / Review)
  - `web/src/components/ReviewTable.tsx`
  - `web/src/components/CredentialsDrawer.tsx`
  - their `.test.tsx`
- Modify: `web/src/api.ts` (endpoints from Tasks 1–5), `web/mock/v3.mjs` (drafts, destinations, teach thread with a
  `review`, credentials POST)

**Interfaces:**
- Consumes: the endpoints from Tasks 1–5; `streams.agent` for the teach thread; the page inbox SSE for arriving
  examples.
- Produces, the UI behaviour:
  - **Resuming:** step state comes from `GET /api/integrations/{name}/draft`, so a reload resumes.
  - **Pre-filling (demo):**
    - With `?from_page=<page id>`, step 1 suggests the intent from the page title.
    - Step 2 remembers the last Airtable base (`localStorage` `pw-last-airtable-base`, wrapped in try/catch).
    - Step 3 lists pages already sent to the draft.
  - **Review table:**
    - One value column per example page, with an "AI" label on AI-proposed rows and a "needs you" status.
    - Each row has a dropdown of `alternatives` plus "Type a value…" and "Skip".
    - Approve sends `{decision:"approve", edits}`.
  - **Credentials drawer:** lists `GET /api/credentials` and adds credentials with a password input. After saving,
    it shows only "set".
  - **AI off:** when Teach returns 409 because AI is off, step 4 shows "Turn AI on to let Patchwork propose a
    mapping" with the AI toggle inline. It never shows a dead end.
  - **Limits:** step 1's help text states the limits that apply to every use case: one page is one record; values
    come from the page, the record, today's date, a constant or code; sources are pages and APIs.

- [ ] **Step 1: Write the failing tests** with the mock:
  - The wizard walks all 4 steps for the products draft.
  - Approve with one edit sends exactly the `edits` payload.
  - The review shows "AI" on rows with `ai_proposed`.
  - The credential value never renders after save.
  - The copy contains none of `JOB_WORDS` from Task 6 (apply the same regex to the rendered text).
- [ ] **Step 2: Run them and confirm they fail.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the checks and confirm they pass.** Run `cd web && npm run -s typecheck && npx vitest run && npm run -s build && node mock/smoke.mjs`.
  Expected: PASS.
- [ ] **Step 5: Commit** with message `Web: New integration wizard, review table with edits, credentials drawer`.

### Task 12: Live on stage, timing, and the run sheet

**Files:**
- Modify:
  - `docs/demo-v2.md`: the new 30 s beat replaces the Workday beat.
  - `web/src/components/Presenter.tsx`: script lines and steps for the new beat; domain-neutral elsewhere.
  - `README.md`: the "New integration" section.
  - `ROADMAP.md`
- Test: manual and live (owner present); `bench/v2/results.json` gains `teach_examples_s` and `teach_examples_calls`.

- [ ] **Step 1:** On a separate workspace and a free port (never 38091), with `PATCHWORK_MODEL=anthropic:claude-haiku-5-5`, run
  `scripts/smoke_v2.py --new-integration products` against the real Airtable base `appH3aUzXPLlI5Y2N`, table
  `Products` (`tblv18VnNvF6wozV6`), credential `AIRTABLE_TOKEN`, using two real product pages from the teammate's
  `fixtures/real_products/`.
  - Expected: every beat `ok`, TEACH ≤ 15 s, ≤ 10 model calls.
  - Delete the integration afterwards with `delete_rows=true`.
  - If TEACH takes more than 15 s: cut the learner step for the second site from TEACH, and let LEARN pick it
    up right after deploy. Note this in `docs/demo-v2.md`.
- [ ] **Step 2:** Update the run sheet, presenter script, README and ROADMAP. The 2-minute total stays ≤ 2:00 when
  timed with the presenter's clock.
- [ ] **Step 3:** Run the full checks:
  - `.venv/bin/python -m pytest -q`
  - both existing smokes
  - `make smoke-new-integration-offline`
  - the web checks
  - the v1 bench

  Expected: all green.
- [ ] **Step 4: Commit** with message `Demo: live new-integration beat (products to Airtable), timings, docs`.

## Order and parallelism

- **Backend lane:** Tasks 1, 2 and 3, then 4, then 5, then 6, 7 and 8 (parallel), then 9.
- **Web lane:** Tasks 10 and 11 can start on the mock right away.
- **Last:** Task 12 needs everything.
- **Files shared with Plan B:** `control/api/recordings.py` and `web/src/pages/NewIntegration.tsx`. Plan B starts
  after Task 4 and Task 11 land.
