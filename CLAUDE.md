# Patchwork: shared notes for every agent and teammate

This is the one file every agent working on Patchwork reads first. It records the rules, the layout, who owns what,
and the live status board.

**Keep it current:**
- Whoever finishes a task updates **Status** (and **Known gaps** if needed) in the same push.
- Push straight to `main`. Pull first: `git pull --rebase origin main`.
- Keep entries short; link to plans and specs instead of copying them.

Last updated: 2026-11-14, by Satvik's agent, after the PR #4 review fixes (outcomes per document).

## What Patchwork is

A hackathon project (tracks: Practical AI, Business/Enterprise; a 2-minute demo). A person shows Patchwork where
data comes from and where it goes. AI learns the mapping once. After that, a compiled, AI-free integration moves
every new record, and holds anything doubtful for a person.

**The pipeline:** a spec (YAML), then the Jinja compiler, then an AI-free runner subprocess. The runner's steps are:
route → extract → validate → dedupe → map → send → verify.

**Sources:**
- pages, sent by the browser extension to the page inbox;
- `api` list endpoints;
- documents (emails, PDFs, sheets): in progress, G4 onwards.

**Destinations:** Google Forms, plain HTML forms, Airtable, OpenAPI APIs (nested bodies), SQLite.

**Quarantine codes:** `PARSE_NO_MATCH`, `VALIDATION_FAILED`, `NOT_FOUND`, `SINK_CONTRACT`, `AUTH`, `VERIFY_MISMATCH`,
`UNKNOWN`.

## Hard rules (never break these)

- **All AI goes through the one deep agent.** That is `deepagents==0.7.38` via `agents/deep.py`
  `DeepAgentService`. Its parts:
  - a `create_deep_agent` supervisor;
  - subagents through the `task` tool;
  - `write_todos`;
  - `FilesystemMiddleware`, with writes limited to `/spec.yaml`, `/examples.yaml`, `/transforms.py`;
  - `EventMiddleware` and `ResilientModelMiddleware`.

  TEACH, LEARN and REPAIR are modes of that agent. New behaviour is a mode or a tool, never a new agent.
- **Guardrails live in tools** (`agents/deep_tools.py`), as code checks, never only in prompts.
  `agents/scripted_v2.py` (the scripted agent) emits the same events, with zero model calls. It is the stage
  fallback and the test default.
- **The runner stays AI-free.** It runs as a subprocess with a stripped environment and no model keys, and refuses
  to start with AI modules loaded. Everything that runs per record is plain code.
- **Never silently write a wrong row.** When in doubt, hold the record for a person. Every AI-produced value is
  checked by code and labelled `ai_proposed` in the UI. No row is dropped or guessed.
- **v1 must keep working unchanged:** supplier POs → Stockroom (`po_to_stockroom`). All existing tests stay green.
  Specs that don't use a new feature must compile byte-identically.
- **Domain-free input layer:** no subject words (invoice, order, ticket, …) in these places:
  - `runner/documents.py`, `runner/imap.py`, `runner/rows.py`, `runner/clean.py`
  - `control/connectors/`, `control/documents.py`
  - the document prompt and the document tool descriptions

  Fixtures, demos and tests may use subject words.
- **Ports:** never bind, kill or reset 8000–8003, 5180 or **38091** (the owner's live stage). Use in-process servers,
  free ports and a separate `PATCHWORK_WORKSPACE`.
- **Secrets:** never print, log or commit `.env` contents or tokens: `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`,
  `AIRTABLE_TOKEN`, Google `AIza…` keys.
  - Before every push, scan the outgoing diff (`git log -p origin/main..HEAD`) for the `.env` values and for the
    patterns `AIza`, `sk-ant-`, `sk-or-`, `pat…`, `ghp_` and `PRIVATE KEY`.
  - No personal data (emails, real names) in commits or fixtures.
- **Tests are offline:** fixtures, fakes and in-process servers. The model budget is tight, so prefer the scripted
  agent and fake models.
- **Demo data:** develop against the demo Form/Sheet and the demo Airtable base, never the owner's real tracker.
- **Commits:** every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` (agents)
  or the author's own trailer.

## Layout

| Path | What it is |
|---|---|
| `spec/` | Pydantic spec models (`spec/models.py`), `load_spec`, spec hash |
| `compiler/` | spec → generated integration package (Jinja templates in `compiler/templates/`) |
| `runner/` | AI-free runtime: `run.py` (run lock, replay), `pipeline.py` (`process_record`, `process_document`), `rows.py`, `clean.py`, `store.py`, `credentials.py`, sinks (`forms.py`, `airtable.py`, `http.py`) |
| `agents/` | `deep.py` (the deep agent), `deep_tools.py` (tools and guardrails), `prompts.py`, `learn.py`, `scripted_v2.py` |
| `control/` | control plane: `api/` (FastAPI server, drafts, pages, credentials, runs), `drafts.py`, `destinations.py`, `page_hooks.py`, `autorun.py` |
| `web/` | React + TypeScript UI. Pages: Home, IntegrationPage, NewIntegration (the wizard). Components include ReviewTable and CredentialsDrawer. `web/mock/v3.mjs` is the mock backend |
| `capture/`, `matcher/` | the recorder and the recording → candidates matcher (plan B, "Show it once") |
| `demo_apps/` | fakes: Stockroom, tracker form, fake Airtable, `fake_papers_api` |
| `fixtures/` | specs, pages, recordings, Google Form fixtures (keys scrubbed) |
| `scripts/smoke_v2.py` | end-to-end smoke (numbered `ok` lines) |
| `docs/contracts.md` | the numbered contracts (V10–V18). Change a contract only together with its code, in the same commit |
| `docs/superpowers/specs/`, `plans/` | design docs and implementation plans (below) |

## Commands

```bash
make setup                          # uv venv (Python 3.12) + deps + playwright chromium
.venv/bin/python -m pytest -q       # full suite, ~6 min
make smoke-v2-offline               # smoke into the demo form, in-process
make smoke-v2-airtable-offline      # same beats into fake Airtable
cd web && npm run -s typecheck && npx vitest run && npm run -s build && node mock/smoke.mjs
make web-mock                       # UI against the mock backend
```

Don't run `make stage-v2` or anything else on port 38091 unless the owner asks.

## Plans and specs

- **v2 base:** `docs/superpowers/specs/2026-11-14-patchwork-v2-design.md` and
  `plans/2026-11-14-patchwork-v2-plan.md`.
- **New integration wizard (plan A):** `specs/2026-11-14-new-integration-flow-design.md` and
  `plans/2026-11-14-new-integration-wizard-a.md`. Tasks 1–12.
- **Show it once (plan B, deferred backup):** `plans/2026-11-14-show-it-once-b.md`.
- **Generalized input (emails, PDFs, sheets, tables):** `specs/2026-11-14-generalized-input-design.md` and
  `plans/2026-11-14-generalized-input.md`. Tasks G1–G15.
- **Teammate plan:** `plans/2026-11-14-teammate-inputs.md`. It holds the `Document` reader contract, the email
  fixtures, and the G4–G7 handoff notes. **Read its "Update (2026-11-14, evening)" section before G4–G7.**

## Ownership

| Who | Owns |
|---|---|
| Lead (Claude + agents) | `web/`; plan A Tasks 9 and 12 (`scripts/smoke_v2.py`, `fixtures/events/`, `fixtures/papers/`, `demo_apps/fake_papers_api/`); reviews and merges; demo prep |
| Satvik (+ agents) | `runner/documents.py`, `runner/imap.py`, `fixtures/emails/`, `scripts/make_email_fixtures.py`, `control/connectors/`, `tests/inputs/`; **G4–G7**, including the `control/`, `agents/` and `runner/pipeline.py` files those tasks name |
| Owner | product decisions, live demo stage (:38091), live rehearsal (Task 12) |

If you need to change a file someone else owns, say so in your PR or commit message and keep the change small.

## Status

### Done (on `main`)

- **v1 and v2 base:**
  - pages → forms/Airtable;
  - LEARN for new sites (platform wildcards, e.g. `*.myworkdayjobs.com`);
  - REPAIR when a destination renames a field;
  - credentials (V13);
  - plain HTML forms (PR #1);
  - SQLite destination (V16, PR #2).
- **Plan A, wizard lane 1 (Tasks 1–4):**
  - credentials POST from the UI;
  - draft integrations (`draft` → `teaching` → `live`);
  - safe delete (409 while busy; destination rows aren't deleted);
  - destination check;
  - teach from example pages.
- **Plan A, wizard lane 2 (Tasks 5–8):**
  - TEACH from examples: `propose_mapping` and `add_site_parser`. One parser per example site, never a catch-all;
    wildcards only for known platforms.
  - `request_deploy` review rows. Each row has `dest_field`, `source`, `values`, `ai_proposed`, `status`,
    `alternatives`, `type`, `required` and `values_allowed`.
  - Review edits are applied by code. A bad edit gets **422** and the review stays open.
  - A "person confirmed" label applies only to the field that was asked about.
  - Deep and scripted agents.
  - API destination with nested body.
  - **Run lock:** runs of one integration never overlap, so no double writes.
- **Plan A, wizard lane 3 (Tasks 10–11):** Home, IntegrationPage, NewIntegration wizard, ReviewTable,
  CredentialsDrawer. The review table hides Skip on required fields, picks choices from a list, and checks typed
  numbers, dates and links the way the backend does.
- **Plan A Task 9, three-domain smoke:** `smoke_v2.py --new-integration {products,events,papers} --in-process`
  (and `make smoke-new-integration-offline`) walks the wizard API with zero model calls:
  - new, destination, 2 pages, teach, approve;
  - rows written;
  - a third site is learned;
  - a destination rename is held, repaired and written.

  The scripted mapper now maps only capture dates (added, captured, saved, seen, sent, applied) to today. Any
  other date is read from the page, or the person is asked.
- **Wizard shows agent questions:** if TEACH stops on `ask_user` (e.g. no example page shows a field's value),
  step 4 shows the question with its options and a free-text answer, instead of spinning forever.
- **Airtable token-ID hint:** if Airtable rejects `AIRTABLE_TOKEN` and the stored value is only the token's ID
  (`pat` + 14 characters, no dot), the destination check says so. Airtable's token list shows the ID; the token
  itself (ID, dot, secret) is shown only once, when the token is created or regenerated.
- **Generalized input G1–G3 (lane G, contracts V18):**
  - the `rows` parser shape (table, css and lines selects);
  - cleaners;
  - cross-checks (`sum_equals`, `product_equals`, `count_equals`);
  - `process_document`: one document → many records, checks run before any write.
- **Generalized input G4–G7 (contracts V17, V18):**
  - G4: the document inbox (`control/documents.py`, `control/inboxes.py`, `POST/GET /api/documents`, SSE stream; a
    multipart upload parsed with the stdlib, so no new dependency). `is_document_spec`; the page hooks, run on
    arrival and the learn trigger work for either inbox; a flagged document is never learned.
  - G5: LEARN checks for documents and rows (`document_outline`, `run_document_parser`, `rows_checks`, document
    evidence, `check_candidate`, per-row golden `expect.rows`). A document auto-deploys only when a declared
    cross-check passed on it.
  - G6: the document prompt, the scripted `propose_rows_parser` (table, else `lines`), `TOOL_DOCS` for the learn
    tools, `tests/test_no_domain_words.py`.
  - G7: `fixtures/tables/` and `tests/control/test_rows_e2e.py`: a CSV becomes one row per data row (a re-upload
    writes nothing), a file or page that does not fit is held with nothing written, a new page layout is learned
    once and its rows written. Real control plane, compiler and runner subprocess, SQLite destination, no model.
- **Last full verification** (2026-11-14, PR #4 head): 921 passed, 15 skipped; both smokes 9/9 OK. The web checks were
  not re-run (no `web/` change in that PR); the last web run was 129 tests, typecheck and build OK.

### In progress

- **`./patchwork` launcher** (lead's agent): one command, one port, auto port choice, the extension finds it.
  It is used for the demo. Design: `docs/superpowers/specs/2026-11-14-one-command-launcher-design.md`.
- **PR #4 (Satvik, G4–G7):** the review's blocking item (outcomes per document in the tracker) and its Minor items are
  fixed and pushed to the same branch; waiting for the lead's re-review.
- **Satvik:** teammate Task 1 (`fixtures/emails/`) and Task 2 (`runner/documents.py`, which adds `pypdf` and
  `openpyxl`). G4–G7 are done against a stand-in reader (`tests/control/standin_readers.py`); once Task 2 lands, run
  `tests/control/test_rows_e2e.py` again: its real-reader variants, the `.xlsx` test and the HTML-table page test
  skip until then and have never been run.

### Next

- **Plan A:** a final whole-plan review, which triages the deferred minors below. Then Task 12: a live, timed
  rehearsal on the Airtable Products table with Haiku. Needs the owner.
- **Generalized input:**
  - G4: document inbox (V17)
  - G5–G6: LEARN and agents on documents
  - G7: tables proof
  - G8–G12: PDF line items, the fixture emails end to end, the four email kinds, the invoices-from-email demo beat
  - G13 (table sources): optional, cut first
  - G14–G15: documents in the wizard
- **Demo prep:**
  - the owner restarts the stage and reloads the extension;
  - run sheet update (a ~30 s products → Airtable beat, TEACH ~15 s);
  - slides, a backup video, README/ROADMAP.
- **Plan B (Show it once):** deferred.

### Owner decisions pending

1. One "Invoice Lines" table for the demo. *(Default, to confirm.)*
2. Layouts with no total to check always need a person's approval before going live. *(Default, to confirm.)*
3. A folder of `.eml` files is enough for the demo; live IMAP stays optional. *(Default, to confirm.)*
4. Table sources are lowest priority. *(Owner: decided.)*
5. Real invoice PDFs are for rehearsal only, kept out of the repo. *(Default, to confirm.)*
6. Should the runner store destination record ids, so that deleting an integration can delete its rows? *(Open.)*

## Known gaps (deferred; fix when you touch the area)

- **Rows and documents:**
  - `runner/documents.py` does not exist yet (teammate Task 2). `POST /api/documents` answers 503 without it, and
    `rows.py` cannot read a `table` select from a plain page record (a document record carries its `tables`).
  - The scripted agent proposes `table` and `lines` selects only, never `css`; a page record has no `tables`, so a page
    layout is learned through `lines` over its text (an HTML table puts one cell on each text line: needs the reader).
  - Table selects have no `skip`, so "Tax"/"Shipping" footers hold the document.
  - A real row equal to the sum of the others is held.
  - `lines` without `after` needs blank or skipped lines above the first item.
  - `number` (unlike `money`) doesn't refuse an ambiguous `1.299`.
  - `_select_css` swallows selector errors as "no rows".
  - `rows._num` strips commas without `parse_amount`.
  - `process_document` duplicates the send/verify/mark logic of `process_record`.
  - `_negative_cases` ignores cross-check rules.
- **Runs:**
  - A RunManager start can wait up to 60 s on the run lock, then fail. `PageHooks._after_deploy` only logs that, so
    the catch-up replay waits for the next page.
  - The subprocess timeout includes the lock wait.
- **Agents:**
  - `deep_tools.py` is ~2,400 lines; split teach-from-examples into its own module.
  - `flatten_body` and `body_to_dotted` detect leaves differently (a body key named `from`/`const` is misread).
  - Nested-body renames aren't auto-repaired.
  - The model-call cap is 16 against a budget of 10.
  - The scripted heuristics are tuned to the fixtures.
  - A newer answer to the same question doesn't clear an older confirmation.
  - Approving with `needs_you` rows is blocked only in the UI, not on the server.
- **TEACH questions can offer placeholder answers:** a live Haiku run asked "What should Price be?" with the option
  `0`. Picking it would make the price 0 on every row (labelled as the person's choice). A tool-level check should
  refuse placeholder options (`0`, empty) for number and required fields.
- **Pages without structured data are hard to teach:** e.g. Amazon product pages have no JSON-LD, and the first
  `.a-offscreen` price span is empty. Prefer stores with JSON-LD `Product` data for demos.
- **Task 9 follow-ups:**
  - Capture words match without a date word ("Coupon Applied" → today; it is labelled AI).
  - Rename pairing can swap fields or pair on type alone; the deploy approval still gates this.
  - Parser ids collide for `*.example.*` / `*.co.uk` sites.
  - `MusicEvent`-style subtypes get no alternatives.
  - The API repair question still uses v1 wording.
  - The fake Airtable table is always "Applications".
- **Flaky under load:** `test_drafts.py::test_delete_is_refused_while_an_agent_thread_is_active`. Its fixture's
  `wait_idle` has a 30 s budget, which ran out once while three suites ran in parallel.
- **Web:** `new URL()` is stricter than Python's `urlsplit` for rare hosts (a port above 65535, an invalid IPv4).
- **Delete:** destination rows can't be deleted yet; this waits on owner decision 6.

## How to finish a task

1. Run the tests that cover your change, then the full suite and both smokes if you touched Python. Run the web
   checks if you touched `web/`.
2. Commit with the trailer.
3. Update **Status** (move the item to Done, with one line on what it gives) and **Known gaps** in this file. Change
   "Last updated".
4. Run `git pull --rebase origin main`, then the secret scan, then `git push origin HEAD:main`.
