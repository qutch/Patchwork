# One place to add and run integrations: design

Date: 2026-11-14. Status: draft for the owner's approval.

## Intent

The owner: "how would a real user be able to add a new integration? right now everything is all over the place
and confusing, we need to centralize everything so it's easy to use and understand for the user."

Today a person cannot add a new page integration from the UI at all. The jobs one is built from a recording
shipped in the repo (`fixtures/recording_jobs`, id `fixture-jobs`). Setting one up touches six places:
- The Record tab (records a live browser; built for v1 app-to-app work).
- The destination, which is only an API parameter (`POST /api/integrations/build` `destination`).
- Credentials, which live only in `.env` and need a control-plane restart.
- The extension's options or popup, which choose where pages go.
- Seven tabs that follow two different integrations: Review, Integration and Runs follow v1 Stockroom, and
  Tracker follows the page integration.
- Agent Activity, a separate tab.

The owner, on scope: "the user should be able to create any supported integration that we have for any sort
of use case, not limited to what we've been testing (job postings)." The flow is domain-free: nothing in it
knows about jobs, products or any other topic.

Success: someone who has never seen Patchwork adds "product pages → an Airtable table" from one obvious
button, sees each choice it made checked and labelled, and then finds everything about that integration on
one page. The first real use is `track_product_prices` → the owner's Airtable base, live on stage, in the
2-minute demo or right after it.

## Goals

1. **Integrations home:** a list of integrations, each card showing its state, plus a **New integration**
   button.
2. **One page per integration** holding everything about it: rows, what needs the person, learned layouts,
   versions, and agent activity.
3. **New integration, "describe and send an example" (approach A, primary):** name it, pick a destination
   and its credential, send one to three example pages, and review and approve what Patchwork proposes. Then
   it is live.
4. **Credentials managed in the UI:** add a credential by name and value. The value is written to `.env`,
   takes effect without a restart, and is never shown again.
5. **The extension finds new integrations by itself:** a draft integration appears in the popup right away,
   so example pages can be sent to it.
6. **"Show it once" (approach B, backup):** an option in the same wizard. Copy one page into a form inside a
   recorded browser. It reaches the same review step. Demo it only if it works reliably live.

7. **Any use case:** any source kind Patchwork supports (pages sent from the extension, or an API it polls)
   combined with any destination kind it supports (Airtable, forms, OpenAPI APIs), about any subject. The
   wizard, the TEACH-from-examples prompt, the tools, the scripted fallback and all UI wording are
   domain-free. Field names and the intent come only from the person and the destination.

## Non-goals

- Recording into Airtable's own web app (B works with form destinations only: Google Form or the demo form).
- Postgres, SQLite and generic HTML forms. These are separate work (the teammate's tasks).
- Redesigning v1 Stockroom's views. v1 becomes one integration card whose page wraps today's Review,
  Integration and Runs views unchanged.
- Multi-user accounts and permissions.
- Limits that apply to every use case, kept as they are and stated in the UI's help text:
  - One page is one record (no list pages → many rows).
  - Values come from the page, the record, today's date, a constant, or plain-code transforms. Nothing needs
    a judgment per record, since the runner is AI-free.
  - Sources are pages and APIs only (no email, PDF or upload).

## Anti-goals (a failure even if it technically works)

- A value written to a destination that no one checked. Every proposed mapping is checked by code and
  labelled "proposed by AI" until the person approves it.
- A credential value appearing in the UI, a log, an event, an API response or git.
- The jobs demo, the v1 demo, the existing smokes or the test suite breaking.
- Job-specific wording or assumptions anywhere in the new flow. The tuned job learner prompt stays only for
  integrations B1's `is_job_integration` recognises.
- A wizard that dead-ends: every failure (credential rejected, table not found, page doesn't contain a
  value) says what to do next.

## Constraints (unchanged project rules)

- **Agents:** all AI work goes through the one Deep Agents service (`agents/deep.py`, deepagents 0.7.38).
  Approach A is a TEACH variant, not a new agent. Guardrails live in tools (`agents/deep_tools.py`).
  `ScriptedAgentService` gets a matching script that emits the same events.
- **Runner:** stays AI-free (subprocess, stripped env, credentials only as `PATCHWORK_CRED__<NAME>`).
- **Holding records:** never silently write a wrong row; when in doubt, hold the record for a person.
- **Ports and secrets:**
  - Never bind, kill or reset ports 8000–8003, 5180 or 38091.
  - Tests use free ports and fakes: `demo_apps/fake_airtable` and `fixtures/products`.
  - `.env` values never printed or committed.
- **Model budget:** Haiku 5.5 via Anthropic. One live TEACH from examples should cost about as much as
  today's TEACH (under 10 calls).

## Approaches considered

- **A. Describe and send an example (picked as primary).** The person gives intent, destination and one to
  three example pages. TEACH proposes each destination field's source on the page, code checks the proposal
  against every example, and the person approves.
  - Pros: no recorded browser, works for every destination kind (form, Airtable, API), and is safe to run
    live.
  - Cons: the AI proposes the mapping instead of observing it, so the review step carries more weight.
- **B. Show it once (picked as backup).** A recorded browser watches the person copy values from a page into
  a form. The existing matcher (`matcher/pages.py`) explains each value, which is how `fixture-jobs` was
  made.
  - Pros: closest to "teach by watching", which is Patchwork's original story.
  - Cons: form destinations only, and a live recording is fragile on stage.
  - Also: the candidates endpoint calls `match(rec)` without the recording directory, so live page
    snapshots may not be found. The plan must check and fix this.
- **C. Write or upload a spec YAML (dropped).** It's what tests do today, but no real user would.

The owner picked A now, with B as a backup that is demoed only if it works well.

## Design

### 1. App structure

```
Patchwork                                   [Credentials] [AI on] [Benchmark]
─────────────────────────────────────────────────────────────────────────────
Integrations                                              [+ New integration]
┌──────────────────────────────┐ ┌──────────────────────────────┐
│ Job postings → tracker       │ │ Track product prices         │
│ pages → Google Form   ● Live │ │ pages → Airtable    ● Draft  │
│ 6 rows today · 0 held        │ │ waiting for example pages    │
└──────────────────────────────┘ └──────────────────────────────┘
┌──────────────────────────────┐
│ Supplier POs → Stockroom     │
│ API → API             ● Live │
└──────────────────────────────┘
```

**Integration page.** It opens from a card.
- **Header:** name, intent, a source → destination badge (`Destination.tsx`), credential status, state, and
  the version that is live.
- **Sections, as in-page tabs:**
  - **Rows:** today's Tracker view.
  - **Needs you:** held records, agent questions and incidents for this integration.
  - **Layouts:** today's Learned layouts.
  - **Versions:** spec diff, approve, rollback.
  - **Activity:** today's Agent Activity, filtered to this integration's threads.
- **v1 Stockroom:** its page shows today's Review, Integration and Runs views as its sections.

**Global items.**
- **Credentials:** a drawer listing name, set or not set, and used by, with "Add credential".
- **AI toggle:** stays where it is.
- **Benchmark:** moves to secondary navigation.
- **Presenter mode:** stays, and opens on one integration.
- **Deep links:** URL hash routing keeps them working (`#/integrations/<name>/<section>`). The old tab hashes
  (`#tracker`, `#agent`, …) redirect to the matching section, so existing run sheets still work.

### 2. New integration (approach A)

One page with four steps. Each step is saved on the server, so a reload or a second device resumes it.

1. **What.** A sentence ("Track product prices") and a name, suggested as a slug of the sentence and
   editable. This creates a **draft integration**.
2. **Where it goes.** Pick a kind:
   - **Airtable:** paste the base URL (the base id is parsed from it). Tables are listed from the Meta API,
     and the person picks one.
   - **Form:** paste the form URL. The Google Form layout works today; generic HTML forms arrive with the
     teammate's work.
   - **API:** a base URL whose OpenAPI is served at `/openapi.json`, then one write operation picked from it
    (see the owner's decisions below).

   Then pick a credential, or add one (name and value). **Check connection** fetches the destination's
   contract and shows its fields with their types, e.g. "Product (text), Price (number), Store (text), Link
   (url), Date Added (date)". Failures say why: the credential was rejected, the table wasn't found, or the
   host couldn't be reached.
3. **Example pages.** "Open a product page and click Send this page in the extension, with this integration
   selected." The draft is already in the popup. Arriving pages appear in a list (title, site) within a
   second or two, through the existing page inbox and its event stream. One page is enough; two or three from
   different sites make the check stronger.
4. **Review what Patchwork proposes.** TEACH runs on the destination's fields and the example pages. It
   proposes a source for each destination field:
   - a page reader (CSS or JSON-LD, the existing parser format)
   - a record field (`record.canonical_url`)
   - today's date
   - a constant
   - skip

   Code checks every proposal before the person sees it (see section 4). A table shows:

   ```
   Destination field   From                         Page 1 (shop-a)      Page 2 (lumen)
   Product             page: h1.product-title       Acme Steel Kettle    Bolt Desk Lamp     proposed by AI ✓ checked
   Price               page: JSON-LD offers.price   24.99                89.00              proposed by AI ✓ checked
   Store               ?  (agent asks)              —                    —                  needs you
   Link                the page's address           https://shop-a…      https://lumen…     ✓
   Date Added          today's date                 2026-11-14           2026-11-14         ✓
   ```

   Anything it can't place becomes a question with options, including "Skip this field" (A2's TEACH question
   mechanism). The person can change any row. **Approve** compiles the spec, makes golden tests from the
   example pages, and deploys version 1. The integration goes Live, and the example pages are run through it,
   so the first rows appear in Rows straight away.

After that, everything works as it does for jobs today: new layouts are learned once per site or platform,
destination changes hold records and are repaired, and people approve.

### 3. Show it once (approach B, backup)

Step 1 offers "or show me once (form destinations)".
- **Recording:** this opens today's recorder (Record tab logic, moved into the wizard) at a start URL. The
  person opens a page, copies values into the form, and submits.
- **Review:** Stop runs the matcher (`matcher/pages.py`), and its candidates flow into the same step 4
  review table.
- **Demo:** before demoing, the plan must make one live recording on the demo Google Form work end to end.

### 4. Backend pieces (names are proposals; the builder may adjust)

- **Draft integrations.**
  - **Storage:** a stored record with name, intent, destination, state (`draft` → `teaching` → `live`),
    example page ids and the TEACH thread id.
  - **Listing:** `GET /api/integrations` includes drafts with their state. `GET /api/extension` lists
    drafts as page integrations, and `POST /api/pages` accepts a draft's name (B1 returns 422 today for an
    unknown name).
  - **Running:** a draft is never run; its pages wait in the inbox.
- **`POST /api/integrations/new`** `{name, intent}` → a draft.
- **`PUT /api/integrations/{name}/destination`** `{kind, ...sink fields, auth?}` → checks the connection and
  returns the contract summary. Airtable table listing: `GET /api/destinations/airtable/tables?base_id=…&credential=…`.
- **`POST /api/credentials`** `{name, value}`.
  - Checks the name with `runner.credentials.check_name` (model keys and process variables are refused).
  - Writes or replaces the line in `.env`: it stays gitignored, and the file is set to mode 0600.
  - Sets `os.environ[name]` in the control plane, so no restart is needed.
  - Returns `{name, set: true}`; it never returns the value.
  - Redaction must know the value at once, so `redact()` also blanks the values of every name in
    `GET /api/credentials` that is set.
- **`POST /api/integrations/{name}/teach`** `{page_ids}`.
  - Writes candidates for a synthetic recording `draft-<name>`: intent, the first example page as `source`
    (kind page; snapshot = its HTML), the destination contract as `sink`, `mappings: []`, and the other
    example pages.
  - Then calls the existing build with `name` and `destination`.
- **TEACH from examples** (a variant of TEACH in the one deep agent, chosen when the candidates have no
  mappings and carry example pages).
  - **New tool `propose_mapping`** (in `agents/deep_tools.py`): field → source. Its code checks:
    - A page-reader value must be found on **every** example page, using the existing LEARN evidence and
      literal-capture checks.
    - The value must fit the destination field's type: number, date, url, or a select's options.
    - A constant is allowed only for a field the person confirms in the review table.
    - Unmatched required fields become questions.
  - **Failures:** a proposal that fails a check is returned to the agent as REFUSED with the reason, the same
    as `save_parser`.
  - **Output:** the review table, with every value labelled as AI-proposed.
  - **Scripted fallback:** the same events and the same tool, with proposals from name matching against
    JSON-LD and `og:` fields (Product → `name`, Price → `offers.price`, Link → canonical URL, Date → today).
- **API sources:** a later wizard option. Approach A covers page sources first. For an API source the "example"
  is the first records from its list endpoint, through the same review table. The plan treats it as an
  optional last task.
- **Approve:** reuses today's compile → test → deploy path. The example pages become golden examples.

### 5. UI pieces

- **New:** `Home` (the integration list), `IntegrationPage` (header + sections), `NewIntegration` (the wizard),
  `CredentialsDrawer`.
- **Reused inside sections:** `Tracker`, `Layouts`, `Agent`, `Review`, `Integration`, `Runs`.
- **Mock:** the mock server (`web/mock`) gains drafts, a wizard walk-through and credentials, so the UI can be
  built and tested without the backend.

### 6. Products → Airtable, the first real use

- **The table:** `Products` in the owner's demo base `appH3aUzXPLlI5Y2N`, with fields Product
  (singleLineText), Price (number, 2 decimals), Store (singleLineText), Link (url) and Date Added (date).
  - **Created:** made on 2026-11-14 through the Meta API (table id `tblv18VnNvF6wozV6`). So `AIRTABLE_TOKEN`
    has `schema.bases:write`, and a later live rename test can use the API instead of doing it by hand.
- **Then:**
  - Run through the wizard live on stage with real product pages (from the teammate's task 2).
  - Use Haiku, and check the cost against the budget.

## Testing

- **Offline end to end:** a new `scripts/smoke_v2.py --new-integration` walks the wizard API against fake
  Airtable with `fixtures/products` pages, on both the scripted and deep agents. The deep agent uses an
  offline fake model in tests.
- **Three domains, one flow:** the same wizard API walk, offline, on three synthetic domains with their own
  destinations. The domains are products → Airtable (`fixtures/products`), events → a form, and research
  papers → an API sink. A test checks that prompts, tool descriptions and UI copy in the new flow contain no
  job words.
- **Tool checks:** `propose_mapping` refuses a value missing from one example page, a literal capture, and a
  wrong type.
- **Credentials:** a sentinel value added through `POST /api/credentials` never appears in responses, events,
  logs or the git index.
- **Web:** tests for Home, IntegrationPage, the wizard steps and redirects of old hashes; typecheck and build
  pass.
- **Regression:**
  - All existing tests pass.
  - `smoke_v2.py --in-process` passes for both form and Airtable.
  - The v1 bench catches 2/2 breaks.

## Owner's decisions after the builder check

- **API destinations are built now,** alongside Airtable and forms.
  - **Choosing the operation:** the wizard reads the target's `/openapi.json`, lists its write operations
    (POST/PUT/PATCH) with their summaries, and the person picks one.
  - **Fields:** the request body's schema is flattened into dotted field paths (`customer.email`), shown as
    rows in the same review table. Required fields come from the schema.
  - **Teaching:** `propose_mapping` maps destination field paths exactly as it maps labels. The compiler
    rebuilds the nested `body` from the dotted paths.
  - **Repair:** goes through the existing OpenAPI diff, as in v1.
  - **Tests:** the papers → API domain covers this path end to end, using a small fake API in
    `demo_apps/`.
- **Demo slot: replace a beat in the 2-minute script.** The Workday/learn beat is cut to make about 30
  seconds for a live "new integration": products → Airtable.
  - **Pre-filling:** to fit, the wizard fills itself from one example page. The intent is suggested from the
    page and the destination remembers the last Airtable base. Step 3 accepts the page already sent from
    the extension.
  - **Timing:** the live TEACH from examples must finish in about 15 s on Haiku. The plan measures it, and
    the run sheet (`docs/demo-v2.md`) and the presenter script are rewritten for the new beat.

## Calls made by the lead (the owner can overrule)

- **One parser per example site, never a shared catch-all.** TEACH from examples does two jobs, both with
  existing guardrails:
  - **TEACH** chooses the integration's fields and maps each destination field to one of them, or to the
    record, today's date, a constant, or skip (`propose_mapping`).
  - **The learner subagent** then learns one parser per example site through the existing `save_parser`.
    That path already has the evidence, corroboration, literal-capture and platform-wildcard checks.

  The review table shows one value column per example page, with that page's own reader. Future sites go
  through LEARN exactly as jobs do today. A catch-all would write rows from unseen sites without LEARN's
  checks, so it is not allowed.
- **Review edits are applied by code, not by the model.** Each row's dropdown holds only choices that already
  passed the checks: the other readers that worked, today's date, the page's address, skip, or a constant
  the person types. Approving with edits applies them as a spec patch through the existing compile → test
  path and then deploys. Edits cost no model calls. The approve step is `request_deploy` with a new optional
  `edits` field, and the scripted agent handles it the same way. No free-typed CSS or JSON-LD paths.
- **v1 app-to-app keeps working through "Show it once".**
  - The recorder (today's Record tab logic, including "Use fixture recording") moves into the wizard's
    "Show it once" option. That option handles what the matcher already handles: API → API (v1) and
    page → form.
  - `#record` redirects there, and the v1 run sheet in `web/README.md` is updated.
- **AI off or the model failing:** the wizard's last step says "Turn AI on to let Patchwork propose a
  mapping". The stage fallback is today's: start the control plane with the scripted agent, which emits the
  same events and makes the same name-based proposals. A wizard with AI on never switches agents
  automatically.
- **Rehearsal:** an integration can be deleted completely: its draft, versions, pages, learned parsers and
  threads; the credential itself stays. Deleting asks whether to also delete the rows Patchwork wrote to the
  destination. Patchwork deletes only rows it can identify by the record ids it stored, never the rest of
  the table.

- v1 Stockroom keeps its current views, wrapped in its integration page.
- Benchmark moves to secondary navigation, and old tab hashes redirect.
- A draft is listed in the extension as soon as it is created.
- Approve runs the example pages straight away, so the first rows appear during the demo.
- B is limited to form destinations.

## Left to the builder

- Exact endpoint and component names, the storage table layout, and visual details within the existing
  design system.
- How the wizard step state is stored: a store row or a JSON file in the workspace.
