# Real-app integrations: Google Sheets, Gmail, Notion and friends

Status: design proposal, no product code changed. Researched 2026-11-14 against the official API docs listed under
[Sources](#sources). Anything marked **(unverified)** was not confirmed in official docs.

## 1. Summary and recommendation

**Today Patchwork only talks to our demo apps** because of four assumptions in the code:

- The source is a JSON `GET` list (`spec/models.py` `Source`).
- The sink publishes `/<base>/openapi.json`, which `runner/run.py` `preflight` hashes before every run.
- There is no auth: `Spec` has no credentials, and `control/ops.py` `runner_env` strips secrets.
- Recording assumes the page's own XHR traffic *is* the API (`capture/recorder.py`, `matcher/core.py`).

None of these is deep. The pipeline itself carries over unchanged to real apps: route → extract → validate → dedupe →
map → send → verify, with quarantine and incidents.

**The central design move** is a *provider adapter*. For each app it supplies four functions: `list_records`,
`fetch_contract`, `send` and `verify`. `fetch_contract` turns whatever schema the app exposes into a small
OpenAPI-shaped document: a Sheets header row, Notion data-source properties, Airtable base schema or HubSpot properties.
That one choice means `runner/contract.py`, the `CONTRACT_CHANGED` incident (`old_openapi`/`new_openapi`), the generated
contract test, and the agents' `read_openapi`/`diff_openapi` tools all keep working untouched.

**Recommendation: build Google Sheets as a *sink* first**, with a service-account credential and the existing Supplier
Inbox as the source. Reasons:

1. The roadmap already floats "Stockroom + Google Sheet" as the destination.
2. Its auth is the easiest Google path. You share the sheet with the service account's email, with no OAuth consent
   screen and no verification ([create-credentials][g-creds]). `spreadsheets` is only a *sensitive* scope, not
   restricted ([Sheets scopes][sheets-scopes]).
3. It gives the most legible live break we could stage. Someone renames the `Quantity` column header in a real Google
   Sheet. Preflight blocks before any write, and the agent proposes the remap for approval. Everyone in the room has
   had a spreadsheet column renamed under them.

**Effort:** about 8–12 developer-hours for one person, plus 30–60 minutes of Google Cloud setup (enable the Sheets API,
create a service account, share a sheet). Per `ROADMAP.md`, code freeze is H32 and "Sheets / real business" is item 2 on
the cut list. So build it only if someone is free *and* it lands behind a separate integration (`po_to_sheet`) before
freeze. Otherwise, pitch it as the next step using this doc.

**Second step: Gmail as a source** (about 1.5–2 days). A normalised Gmail message (`id`, `sender_domain`, `subject`,
`body` as text) has exactly the shape of a Supplier Inbox message. The existing regex parsers, routing and
`NEW_SOURCE_FORMAT` incident therefore work on real email. The cost is OAuth: `gmail.readonly` is a *restricted* scope
([Gmail scopes][gmail-scopes]). A demo app in "Testing" status works for up to 100 test users, but their refresh tokens
expire after 7 days ([OAuth audience][g-testing]).

**Three biggest risks**

1. **Google OAuth policy for Gmail.** Restricted scopes need an annual CASA security assessment for a public app
   ([restricted verification][g-restricted]). Testing-mode tokens die after 7 days. Fine for a demo; a real product
   decision later.
2. **Sheets has no row ids and low write quotas.** The limit is 60 writes per minute per user
   ([Sheets limits][sheets-limits]), and appends are positional. Without a key column and a read-before-write check, a
   crash between append and "mark processed" duplicates a row. A naive one-call-per-record run of 150 POs also takes
   more than 2.5 minutes.
3. **Secrets and data exposure.** Tokens must reach the runner without entering its environment, logs or the LLM. Even
   if tokens are handled perfectly, `fetch_source_samples` sends **real email bodies to the model provider**. Use a
   dedicated demo inbox. The data policy of the free OpenRouter endpoint is **(unverified)**.

---

## 2. Per-app findings

### 2.1 At a glance

| App | Role | Read / write endpoints | Auth a demo can use | Contract source | Incremental reads | Fit for Patchwork |
|---|---|---|---|---|---|---|
| **Google Sheets** | sink (source possible) | `values.append`, `values.get`, `spreadsheets.get` ([REST][sheets-rest]) | Service account shared on the sheet ([g-creds]) | Header row + sheet properties; Discovery doc ([sheets-rest]) | No row ids; key column or developer metadata ([metadata][sheets-meta]) | **High.** First build |
| **Gmail** | source | `messages.list` (`q`), `messages.get` (`format=full`), `history.list` ([list][gmail-list], [history][gmail-history]) | OAuth desktop loopback + PKCE ([native][g-native]); testing mode, restricted scope | None (no schema). Drift = new formats → `NEW_SOURCE_FORMAT` | Immutable message `id`; `historyId` cursor; 404 → full resync ([sync][gmail-sync]) | **High.** Second build |
| **Notion** | both | `POST /v1/data_sources/{id}/query`, `POST /v1/pages` (parent `data_source_id`), `GET /v1/data_sources/{id}` ([upgrade 2025-09-03][notion-ds]) | Internal connection token; share the page with the connection ([authorization][notion-auth]) | Data-source `properties` by **stable id** ([property object][notion-prop]) | `last_edited_time` filter + `start_cursor` ([filter][notion-filter]); webhooks incl. `data_source.schema_updated` ([webhooks][notion-hooks]) | **High.** Third; easiest auth |
| **Airtable** | both | list/create/upsert records ([list][at-list], [upsert][at-upsert]) | Personal access token with scopes ([scopes][at-scopes]) | `GET /v0/meta/bases/{id}/tables` ([schema][at-schema]) | `rec…` ids, `LAST_MODIFIED_TIME()`; webhooks with cursor ([webhooks][at-hooks]) | High; native upsert = idempotent sink |
| **HubSpot** | both | objects create, `batch/upsert` by `idProperty`, search ([contacts][hs-contacts], [search][hs-search]) | New-platform app, `static` auth, private distribution ≤ 10 accounts ([auth][hs-auth]); free test accounts ([test accounts][hs-test]) | Properties API ([properties][hs-props]) | Search on `lastmodifieddate`/`hs_lastmodifieddate`; 10k result cap | Medium. Good contract story; Zapier covers it well |
| **Outlook + Excel (Microsoft Graph)** | mail source, Excel sink | `messages/delta`, `tables/{t}/rows/add`, `tables/{t}/columns` ([delta][ms-delta], [rows][ms-rows]) | Delegated OAuth, device code flow ([device code][ms-device]) | Excel table columns; Graph CSDL/OpenAPI ([msgraph-metadata][ms-meta]) | `deltaLink` per folder | Medium. Mirror of Gmail→Sheets for M365 shops |
| **Shopify** | both | GraphQL Admin only for new apps ([REST legacy][sh-rest]); `orders(query: "updated_at:>…")` ([orders][sh-orders]) | Dev Dashboard app, client credentials, 24 h tokens ([dev dashboard][sh-dev]) | GraphQL introspection for a pinned version ([versioning][sh-ver]) | Cursor pagination, 25k cap ([limits][sh-limits]) | Later. Compiler needs a GraphQL template |
| **Slack** | sink (source limited) | `chat.postMessage`, `conversations.history` ([postMessage][sl-post], [history][sl-hist]) | Bot token `xoxb-` in an internal app ([tokens][sl-tokens]) | None usable: archived OpenAPI 2.0 ([specs repo][sl-specs]) | `oldest` + cursor | Low. Notifications are a solved problem |

### 2.2 Details and gotchas per app

**Google Sheets**

*Writes*
- `values.append` writes after the last row of the "table" it detects in the range ([append][sheets-append]).
- Use `valueInputOption=RAW`. `USER_ENTERED` parses text as if typed, so `1-2` becomes a date and `=…` becomes a
  formula, which is a formula-injection risk from email text ([ValueInputOption][sheets-vio]).
- The append response returns `updates.updatedRange` and `updatedRows` ([UpdateValuesResponse][sheets-uvr]). That is
  enough to verify the write without a second read.

*Row identity and positional writes*
- There is no native row id (**unverified** as an explicit statement; none is documented).
- Developer metadata can be attached to rows and stays with them "as the spreadsheet is edited"; behaviour under sort
  is **(unverified)** ([metadata][sheets-meta]).
- For the demo, a visible **key column** (e.g. `Reference`) is simpler and auditable.
- Appends are positional (arrays of cells). The adapter must order values by the *live* header, not the compiled one.

*Quotas*
- 300 reads/min per project and 60/min per user; the same for writes. Over quota returns 429; use truncated
  exponential backoff ([limits][sheets-limits]).
- A batch request counts as one request.
- With a service account, the "user" is the service account.

**Gmail**

*Listing and incremental sync*
- `messages.list` returns only `id` and `threadId`, so every record costs an extra `messages.get`.
- `q` uses Gmail search syntax but is not allowed with the `gmail.metadata` scope ([list][gmail-list]).
- `history.list` needs `startHistoryId`. History is "typically valid for at least a week, but in rare cases … only a few
  hours", and an expired id returns 404, which means do a full sync ([history][gmail-history], [sync][gmail-sync]).

*Bodies*
- Bodies are MIME trees: `payload.parts[]`, where `body.data` is **base64url**. Large parts carry an `attachmentId`
  instead ([Message][gmail-msg], [attachments][gmail-att]).
- The connector must pick `text/plain`, else strip `text/html`, then normalise whitespace before regexes see it.
  Quoted replies and forwarded chains will confuse regexes; strip `>`-quoted lines.

*Ids, quotas, push and service accounts*
- Message `id` is immutable ([Message][gmail-msg]), which makes it a good `record_id`.
- Quota costs: 6,000 units/min per user; `messages.list` costs 5 units; `messages.get` costs 20
  ([quota][gmail-quota]). A higher cost for `format=full`/`raw` (60 units) is **(unverified)**. Either way, a 150-message
  backfill fits well inside one minute's quota.
- Push (`users.watch` → Pub/Sub) needs a re-`watch` at least every 7 days and can drop notifications. Keep polling
  `history.list` as the fallback ([push][gmail-push]).
- Service accounts cannot read user mail without Workspace domain-wide delegation ([auth overview][g-authov]). That they
  cannot read consumer @gmail.com at all is **(unverified)** as an explicit statement.

**Notion** (current `Notion-Version` is `2026-03-11` ([changelog][notion-changelog]))

*Endpoints*
- Since 2025-09-03, rows live in **data sources**: `GET /v1/databases/{id}` lists `data_sources`; query rows with
  `POST /v1/data_sources/{id}/query`; create rows with `POST /v1/pages` and
  `parent: {type: "data_source_id", …}` ([upgrade guide][notion-ds]).
- Any connector built from older tutorials (`databases.query`) is out of date.

*Schema, ids and limits*
- "A property's `id` stays the same when its name changes" ([property object][notion-prop]). Key the contract on ids,
  so a rename is not a break.
- Property values are nested shapes such as `{"Qty": {"number": 20}}` and title/rich-text arrays. Rich text is capped at
  2,000 characters, and a request at 500 KB ([limits][notion-limits]).
- Rate limit since Sept 2026: 180 requests/min per connection on non-Business plans (600 on Business/Enterprise), plus a
  shared per-workspace limit ([limits][notion-limits]).
- A query returns at most 10,000 results ([query][notion-query]).

*Auth*
- A page must be shared with the connection, or reads return 404 ([authorization][notion-auth]). This is the most
  common setup failure.

**Airtable**

- Reads use offset pagination with `pageSize` ≤ 100 ([list][at-list]). The rate limit is 5 req/s per base; a 429 means
  wait 30 s ([rate limits][at-rate]).
- `performUpsert.fieldsToMergeOn` gives an **idempotent** write keyed on our dedupe key ([upsert][at-upsert]). A crash
  between write and "mark processed" then cannot duplicate.
- Field ids surviving renames is **(unverified)** in the API docs, though Airtable recommends keying code on field ids
  ([finding IDs][at-ids]).
- The 10-records-per-request limit appears only in a support article ([troubleshooting][at-trouble]).

**HubSpot**

*Versioning and auth*
- APIs are date-versioned since March 2026 (e.g. `/crm/objects/2026-09/…`), and v3 paths remain "legacy"
  ([versioning][hs-ver]).
- Legacy private apps can no longer be created after 2026-10-26 ([private apps][hs-private]). Use a new-platform app
  with `static` auth ([auth][hs-auth]).

*Writes, reads and schema*
- `batch/upsert` by a unique property is idempotent.
- Search: 5 req/s, at most 200 per page and 10,000 results per query ([search][hs-search]). An incremental reader must
  advance a time window rather than page deeper.
- The Properties API gives types and enumeration options ([properties][hs-props]). HubSpot's OpenAPI files are
  published but "not licensed for external use" ([spec repo][hs-spec]), so use the Properties API for contracts.

**Microsoft Graph (Outlook mail, Excel)**

*Outlook mail*
- Message ids change when a message moves unless you send `Prefer: IdType="ImmutableId"` ([message][ms-msg]).
- Bodies are HTML by default; `Prefer: outlook.body-content-type="text"` returns text ([send][ms-body]).

*Excel*
- Write rows with `rows/add`; read the schema from table columns ([rows][ms-rows]).
- Workbook sessions expire after about 5–7 minutes idle ([excel][ms-excel]).
- Excel on personal OneDrive is contradictory in the docs, so **(unverified)**. Plan on a work or school account.

*Throttling*
- 429 with `Retry-After`. Outlook allows 10,000 requests per 10 minutes per mailbox ([limits][ms-limits]).

**Shopify**

- REST Admin has been legacy since 2024-10-01, and new public apps must use GraphQL ([REST][sh-rest]).
- **Versioning trap:** a request for an expired version is silently served the oldest supported version. Check the
  `X-Shopify-API-Version` response header in preflight ([versioning][sh-ver]).
- Since 2026-04, `inventoryAdjustQuantities` requires an `@idempotent` key ([mutation][sh-inv]). That is good for us.
- Custom apps can no longer be created in the store admin since 2026-01-01 ([changelog][sh-custom]). Dev Dashboard
  client-credential tokens expire after 24 h ([dev dashboard][sh-dev]), which our credential broker handles naturally.

**Slack**

- As a source: since 2025-05-29, *commercially distributed, non-Marketplace* apps get 1 `conversations.history`
  request/min with at most 15 messages. Internal custom apps are exempt ([changelog][sl-rate]).
- As a sink, `chat.postMessage` (about 1 msg/s per channel) is trivial and has no schema to protect. Patchwork adds
  little here.

---

## 3. Design

### 3.1 Provider adapters (the one abstraction)

Each provider is a Python module with a fixed interface:

```python
# runner/providers/google_sheets.py   (pure HTTP over runner.http.Http; never imports an LLM SDK)
def fetch_contract(http, cfg) -> dict           # OpenAPI-shaped doc synthesised from the live schema
def list_records(http, cfg, cursor) -> list[dict]   # source side; returns flat, normalised records
def send(http, cfg, body: dict) -> Response     # body is the flat dict build_body() already produces
def verify(http, cfg, fields, record, resp) -> None   # raise Quarantine("VERIFY_MISMATCH", …)
def existing_keys(http, cfg) -> set[str]        # optional: read-before-write idempotency
```

**Generated code stays a frozen snapshot.** Versions are immutable and self-contained today. The compiler must
therefore **copy the provider module into the version package** (as it does `transforms.py`) and put its hash in
`manifest.json`. Otherwise upgrading `runner/` would silently change how old versions behave.

**Synthesised contracts.** Here is what `fetch_contract` returns for a sheet whose header is
`SKU | Quantity | Reference | Supplier`:

```json
{"openapi": "3.1.0", "info": {"title": "google_sheets 1AbC…/Incoming", "version": "header-row-1"},
 "paths": {"/rows": {"post": {"requestBody": {"content": {"application/json": {"schema": {
   "type": "object", "additionalProperties": false,
   "properties": {"SKU": {"x-column": "A"}, "Quantity": {"x-column": "B"},
                  "Reference": {"x-column": "C"}, "Supplier": {"x-column": "D"}}}}}}}}}}
```

How each kind of sheet change plays out:

- **Rename or delete a mapped column.** The body has `Quantity` but the contract no longer does, so `validate_body`
  reports "not accepted by destination (no such property)". Preflight blocks and opens a `CONTRACT_CHANGED` incident.
  This is exactly Break 1's path.
- **Add a column, or reorder.** The hash changes, but the golden bodies still validate, so the run proceeds. `send`
  orders cells by the live header.
- **Caveat:** `runner/contract.py` `_TYPES.get(t)` assumes `type` is a string. Synthesisers must emit a single type, or
  `anyOf`, never a JSON-Schema type list.

The manifest's `sink_call` becomes a virtual `POST /rows`, so `validate_body(doc, "POST", "/rows", body)` needs no
change.

### 3.2 Spec changes

Make `source` and `sink` Pydantic discriminated unions on `kind`. Keep the closed vocabulary: each provider has a
typed config with `extra="forbid"`. Every external call gets a `credential:` that is an **id in the broker, never a
secret**.

Bump `spec_version` to 2. A v1 spec loads as `kind: api` plus `contract: {kind: openapi, path: /openapi.json}`, so
`fixtures/spec.yaml` keeps working.

**Example: Gmail order emails → Google Sheet**, reusing the fixture's parsers:

```yaml
spec_version: 2
name: po_email_to_sheet
intent: "For every purchase order email, add a row to the Incoming sheet"
source:
  kind: gmail
  credential: gmail-orders          # broker id; the runner gets a short-lived token, the agent gets nothing
  query: 'from:(acme.com OR globex.com OR initech.com) -in:chats'
  record_id: id                     # Gmail message id (immutable)
  text_field: body                  # connector decodes MIME/base64url -> text/plain, strips quoted replies
  cursor_field: internal_date       # phase 2b: history_id via users.history.list, full resync on 404
  # normalised record: {id, thread_id, internal_date, from, sender_domain, subject, body}
extract:
  route_by: sender_domain
  parsers:                          # unchanged from fixtures/spec.yaml
    - id: acme_v1
      match: {sender_domain: acme.com}
      fields:
        po_id: {regex: 'PO Number:\s*(PO-\d+)'}
        sku:   {regex: 'SKU:\s*([A-Z0-9-]+)'}
        qty:   {regex: 'Qty:\s*(\d+)', cast: int}
    # globex_v1, initech_v1 ...
validate:
  - {field: qty, rule: positive_int}
  - {field: sku, rule: regex, pattern: '^[A-Z]{3}-\d+$'}   # 'exists' stays api-only in phase 1
dedupe: {key: [po_id]}
sink:
  kind: google_sheets
  credential: sheets-writer         # service account; the sheet is shared with its email
  spreadsheet_id: 1AbC...           # recorded from the URL docs.google.com/spreadsheets/d/<id>/edit
  sheet: Incoming
  staging_spreadsheet_id: 1XyZ...   # dry runs and agent tests write here, never to live
  header_row: 1
  value_input: raw                  # never user_entered (formula injection)
  key_column: Reference             # must be a body key; read once per run for idempotency
  body:                             # keys are column headers (compiler renders them with repr, spaces OK)
    SKU:       {from: sku}
    Quantity:  {from: qty}
    Reference: {from: po_id}
    Supplier:  {from: record.sender_domain}
  verify: {expect_rows: 1}          # checked from the append response's updates.updatedRows
on_error: quarantine
```

The generic HTTP sink gains auth and an explicit contract location, so a real REST API with a published OpenAPI also
works:

```yaml
sink:
  kind: api
  base_url: https://api.example.com
  auth: {credential: example-prod, scheme: bearer}       # or {scheme: header, name: X-Api-Key}
  contract: {kind: openapi, url: https://api.example.com/openapi.json}   # or {kind: none} -> run allowed, warned
```

New validation rules:

- `key_column` must be a `body` key.
- `staging_*` is required before `dry_run`, which keeps the "agents never write to live" guarantee.
- `credential` must name a credential whose provider matches `kind`. The check is done by the control plane at compile
  time, not by the pure validator.

### 3.3 Credentials: a broker in the control plane

The rule we keep: **model keys never reach the runner, and the LLM never sees a secret.** The new rule we add:
**provider secrets never reach the runner either. Only short-lived, host-pinned access tokens do.**

```
control plane (holds secrets)                         runner subprocess (stripped env, no LLM)
  control/credentials.py                                 python -m runner run <name>
   store: workspace/credentials.db (Fernet-encrypted,      reads ONE JSON line from stdin before anything:
          key from PATCHWORK_CREDENTIALS_KEY)               {"sheets-writer": {"token": "ya29…", "hosts":
   mint(cred_id) -> {token, expires_at, hosts}                ["sheets.googleapis.com"], "expires_at": …}}
     google SA:   sign JWT -> 1 h access token             Http attaches Authorization ONLY to requests
     oauth:       refresh_token -> access token             whose host is in that credential's hosts
     static PAT:  passed as-is (cannot be down-scoped)     never logs headers; tokens die with the process
  run_sandboxed / control/api/runs.py: Popen(stdin=PIPE), write tokens, close stdin
```

- **Why stdin rather than env or a file:** `runner_env` keeps stripping, and nothing lands on disk or in `ps`/`/proc`
  environ. A token outlives the run by at most its TTL, which is 1 h for Google access tokens.
- **Agents** never call providers directly. Their tools (`read_contract`, `fetch_source_samples`, `diff_contract`) call
  control-plane functions that mint and use the token server-side and return only data. Generated tests use `FakeHttp`
  and a contract file, so they need no tokens.
- **OAuth flow** (phase 2):
  1. `POST /api/credentials/{provider}/connect` returns an authorize URL with PKCE and `state`.
  2. The redirect goes to `http://127.0.0.1:8000/api/oauth/callback`. Loopback redirects are allowed for desktop apps
     ([native][g-native]); web-server apps need `access_type=offline` and `prompt=consent` to get a refresh token
     ([web server][g-web]).
  3. `GET /api/credentials` returns metadata only (id, provider, scopes, account email, expiry), never a token.
- **Approval covers access.** The `request_deploy` card must show which credentials and scopes the version uses
  ("writes to *Incoming* in spreadsheet 1AbC… as sheets-writer").
- **Auth failures are not agent work.** A 401/403 already quarantines as `AUTH`. Add an incident type
  `CREDENTIAL_INVALID` that the repair agent refuses ("reconnect the account"). Google refresh tokens die for many
  reasons, including 6 months unused, a password change with Gmail scopes, and the 7-day testing expiry
  ([OAuth overview][g-oauth]).
- **What this does not solve:** the LLM still reads *data* (sample emails, sheet rows) during BUILD and REPAIR. Gate
  `fetch_source_samples` behind a per-integration "agent may read samples" flag. Use a demo inbox, and a provider with
  a no-retention policy for real customers.

### 3.4 Recording on real sites

**What breaks.** The recorder keeps only same-origin XHR/fetch on allowlisted hosts. The matcher assumes the last
mutating request's JSON body is the destination record and that GET JSON responses hold the source record. On Gmail,
Sheets and Notion the page talks to private, batched endpoints whose payloads don't match the public API. So the
matcher would find nothing or the wrong thing. We did not verify each app's internal protocol, but none of them
documents its web-client traffic as a public API.

**Option A: DOM-level recording plus official-API state diff (recommended to build).** Record what the user *does*:
URLs, copy, paste, typed values. Then let the control plane read the official API around the recording:

1. **Identify objects from URLs.**
   - Sheets: `/spreadsheets/d/{id}/edit#gid={sheetId}`.
   - Notion: the page or database id in the URL.
   - Airtable: `app…/tbl…/rec…` ids ([finding IDs][at-ids]).
   - Gmail web URLs carry a thread token that may not equal the API `threadId` **(unverified)**. Instead, locate the
     source message by searching the copied text with `messages.list?q="<exact phrase>"`.
2. **Snapshot the destination before and after** through the official API (Sheets `values.get`, Notion query with
   `last_edited_time ≥ start`, Airtable list). The diff is the new row, which *is* the sink body keyed by column name.
3. **Emit both as synthetic `net` events** into `recording.json`: the official source record as a GET response, the
   new row as a POST body. `matcher/core.py` then runs unchanged and produces the same `candidates.json`.

**Option B: connector picker plus "paste one example".** A form: choose source and destination from a list, pick the
sheet or data source, paste one example email, and fill in the row you would want. This gives the agent the same three
things: the objects, one labelled example, and the field mapping.

**Candidly, what recording adds over Option B:**

- Recording's real value is **a labelled example for unstructured text**: which substring of which email became which
  column. That becomes a golden test and the seed for regex parsers.
- It also discovers object ids from URLs, and it is a better demo.
- For **structured → structured** flows (Airtable → Notion, HubSpot → Sheet), recording adds almost nothing over a
  mapping form, and Zapier-style field pickers are already good at those.

Build Option B as the dependable path and Option A as the "wow" path. Both feed the same `candidates.json`.

On Sheets specifically, the grid is not ordinary DOM inputs (canvas rendering is **(unverified)** for Sheets; Google
announced it for Docs ([Docs canvas][docs-canvas])). So typed-value capture there is unreliable, and the state diff in
Option A is what makes the Sheets side work.

### 3.5 Contract checks and incidents per provider

| Provider | `fetch_contract` reads | Breaking (block before any write) | Non-breaking (hash changes, run proceeds) | Extra preflight |
|---|---|---|---|---|
| Google Sheets | `values.get Sheet!1:1`; `spreadsheets.get?fields=sheets.properties` | mapped header renamed or removed; tab renamed or deleted | column added or reordered | header row empty → block |
| Notion | `GET /v1/data_sources/{id}` → `{prop_id: type, options}` | mapped property id gone; type changed; select option we write removed | rename (keyed by id); new properties | `Notion-Version` pinned in manifest; `data_source.schema_updated` webhook can trigger an early check later |
| Airtable | `GET /v0/meta/bases/{base}/tables` | mapped field id gone; type changed | rename; new fields | `returnFieldsByFieldId=true` on reads |
| HubSpot | `GET …/properties/{objectType}` | property deleted; type changed; enum option removed | new properties | warn when the pinned API version nears its 18-month unsupported date ([versioning][hs-ver]) |
| Excel (Graph) | `GET …/tables/{t}/columns` | mapped column gone | added column | — |
| Shopify | introspection of the mutation's input type at the pinned version | input field removed or retyped | — | **`X-Shopify-API-Version` ≠ pinned → block** (silent fallback) |
| Gmail (source) | none | — | — | token valid; `history.list` 404 → full resync, not an incident |
| Slack (sink) | `conversations.info` (channel exists, bot is a member) | channel archived or bot removed → `CREDENTIAL_INVALID`-style | — | — |

Incident types after this change:

- `CONTRACT_CHANGED` (sink, today).
- `SOURCE_CONTRACT_CHANGED`, new: a structured source (Notion, Airtable, HubSpot) lost a property we read. Same diff
  machinery.
- `NEW_SOURCE_FORMAT` (today; this is the Gmail story).
- `CREDENTIAL_INVALID`, new, and never agent-repairable.
- Runtime `429`s are not incidents. Add `Retry-After`/exponential backoff to `runner/http.py`, with a cap that
  quarantines as `UNKNOWN` (or a new `RATE_LIMITED` code) when exceeded.

---

## 4. Phased plan

### Phase 1: Google Sheets sink (hackathon if time allows; ~8–12 h, 1 developer)

Scope: the Supplier Inbox becomes the source for a second integration, `po_to_sheet`, alongside `po_to_stockroom`. The
sink is a real Google Sheet with a staging copy and a service-account credential. Break 1 is staged by renaming a
header in the browser.

| File | Change | Est. |
|---|---|---|
| `spec/models.py` | `Sink` → discriminated union (`ApiSink`, `GoogleSheetsSink`); `credential` field; `key_column` in body check | 1 h |
| `compiler/compile.py`, new `compiler/templates/adapters_google_sheets.py.j2` | choose adapter by `sink.kind`; copy provider module into the package; manifest `contract.kind`, `credentials`, virtual `sink_call` | 2 h |
| new `runner/providers/google_sheets.py` | `fetch_contract` (synthesised doc), `send` (append, RAW, live header order), `verify` (from `updatedRows`), `existing_keys` | 2 h |
| `runner/run.py` | preflight calls `integ.fetch_contract(http)`; the `api` adapter keeps `GET /openapi.json`; keys from `existing_keys` count as duplicates | 0.5 h |
| `runner/http.py`, `runner/__main__.py` | absolute provider hosts; read credentials from stdin; host-pinned `Authorization`; 429 backoff | 1 h |
| new `control/credentials.py`; `control/ops.py`, `control/api/runs.py`, `control/__main__.py` | service-account store and mint; `fetch_sink_openapi` → `fetch_contract`; pass tokens on stdin; `python -m control credentials add` | 1.5 h |
| `agents/deep_tools.py` (`read_openapi`, `diff_openapi`) | go through `control.ops.fetch_contract` instead of `<base>/openapi.json` | 0.5 h |
| `tests/` + `fixtures/spec_sheets.yaml`, `fixtures/examples_sheets.yaml` | synthesiser, rename-blocks, reorder-passes, idempotency with `FakeHttp`; no network in CI | 2 h |

**Demo constraint:** at 60 writes/min, keep the Sheet run to about 20–40 records, or batch appends (phase 3). Stockroom
stays the 150-record benchmark.

### Phase 2: Gmail source and OAuth (~1.5–2 days)

- New `runner/providers/gmail.py`:
  - `list_records` uses `messages.list` + `get`.
  - MIME normalisation: base64url decode, text/plain over HTML, strip quoted replies.
  - Cursor: `internal_date`, then `history_id` with a full-resync fallback.
- OAuth connect and callback endpoints in `control/api/`, an encrypted credential store, and a credentials panel in
  `web/`. Add a `CREDENTIAL_INVALID` incident.
- Demo: a dedicated Gmail account in Testing mode with the team as test users. Re-consent within 7 days of the
  presentation. Live Break 2 is sending an email in a new supplier format from a phone.

### Phase 3: connector framework, Notion and Airtable (~1–2 weeks)

- Formalise the provider interface and conformance tests. Add Notion and Airtable as both source and sink, plus
  `SOURCE_CONTRACT_CHANGED`.
- Batch writes: buffer bodies, flush N per call, then mark processed. Native upserts (Airtable, HubSpot) for
  idempotency.
- Recording Option A (synthetic events from official-API snapshots) and Option B (picker + example form) in `capture/`
  and `web/`.
- Per-run egress allowlist: the runner may reach only its providers' hosts. This is a real step up from today's light
  sandbox.

### Phase 4: breadth on demand

- HubSpot (date-versioned paths, Properties API) and Microsoft Graph (Outlook delta + Excel rows).
- Shopify needs a GraphQL adapter template and version-header pinning.
- Push triggers (Gmail `watch`, Notion and Airtable webhooks) to replace polling.
- Multi-user credential ownership and audit log.

---

## 5. Competitive honesty

Zapier, Make and n8n already have Gmail, Sheets, Notion, Airtable, HubSpot, Slack and Shopify connectors, with OAuth,
triggers and field pickers. We have not tested how each behaves when a destination column is renamed. So **do not claim
in the pitch that they write bad data**: say we block *before* the write, and that we verified this ourselves.

**Where Patchwork adds value for these apps**

- **Messy text in, strict schema out.** Order emails in several supplier formats become rows. Patchwork compiles
  tested, readable parsers once and runs them with zero LLM calls, instead of a per-run AI step whose output can drift
  and costs money per record.
- **The maintenance loop.** When a run breaks (new email format, renamed column, changed property type), the run
  blocks or quarantines *before* the write. Patchwork opens an incident and proposes a tested repair as a diff, a
  human approves it, and the quarantined records are replayed. This is the actual product.
- **Versioned, inspectable artifacts.** Immutable versions, diffs, rollback, generated tests, exact accuracy against
  ground truth. These matter to teams who must explain why row 412 is there.

**Where Patchwork would be redundant**

- Plain field-to-field syncs between structured apps (Airtable → Notion, HubSpot → Sheet) and notifications
  ("post to Slack when…"). Mature connectors do these well, and our recording adds nothing.
- Connector breadth, OAuth app verification and trigger infrastructure. We should not compete on the number of apps.

**Positioning:** a few deep connectors where the *data is messy* (email, documents, free-text fields) and the
*destination is strict* (sheets with fixed columns, CRMs with typed properties). Sell the maintenance loop, not the
connector list.

---

## Sources

Google
- `g-creds`: https://developers.google.com/workspace/guides/create-credentials
- `g-authov`: https://developers.google.com/workspace/guides/auth-overview
- `g-oauth`: https://developers.google.com/identity/protocols/oauth2
- `g-native`: https://developers.google.com/identity/protocols/oauth2/native-app
- `g-web`: https://developers.google.com/identity/protocols/oauth2/web-server
- `g-testing`: https://support.google.com/cloud/answer/15549945
- `g-restricted`: https://support.google.com/cloud/answer/13465431
- `sheets-rest`: https://developers.google.com/workspace/sheets/api/reference/rest (Discovery: https://sheets.googleapis.com/$discovery/rest?version=v4)
- `sheets-append`: https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/append
- `sheets-vio`: https://developers.google.com/workspace/sheets/api/reference/rest/v4/ValueInputOption
- `sheets-uvr`: https://developers.google.com/workspace/sheets/api/reference/rest/v4/UpdateValuesResponse
- `sheets-scopes`: https://developers.google.com/workspace/sheets/api/scopes
- `sheets-limits`: https://developers.google.com/workspace/sheets/api/limits
- `sheets-meta`: https://developers.google.com/workspace/sheets/api/guides/metadata
- `gmail-list`: https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list
- `gmail-msg`: https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages
- `gmail-att`: https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages.attachments
- `gmail-history`: https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.history/list
- `gmail-sync`: https://developers.google.com/workspace/gmail/api/guides/sync
- `gmail-push`: https://developers.google.com/workspace/gmail/api/guides/push
- `gmail-scopes`: https://developers.google.com/workspace/gmail/api/auth/scopes
- `gmail-quota`: https://developers.google.com/workspace/gmail/api/reference/quota
- `docs-canvas`: https://workspaceupdates.googleblog.com/2021/05/Google-Docs-Canvas-Based-Rendering-Update.html

Notion
- `notion-changelog`: https://developers.notion.com/page/changelog
- `notion-ds`: https://developers.notion.com/docs/upgrade-guide-2025-09-03
- `notion-query`: https://developers.notion.com/reference/query-a-data-source
- `notion-filter`: https://developers.notion.com/reference/post-database-query-filter
- `notion-prop`: https://developers.notion.com/reference/property-object
- `notion-auth`: https://developers.notion.com/docs/authorization
- `notion-limits`: https://developers.notion.com/reference/request-limits
- `notion-hooks`: https://developers.notion.com/reference/webhooks-events-delivery

Airtable
- `at-list`: https://airtable.com/developers/web/api/list-records
- `at-upsert`: https://airtable.com/developers/web/api/update-multiple-records
- `at-schema`: https://airtable.com/developers/web/api/get-base-schema
- `at-scopes`: https://airtable.com/developers/web/api/scopes
- `at-rate`: https://airtable.com/developers/web/api/rate-limits
- `at-hooks`: https://airtable.com/developers/web/api/webhooks-overview
- `at-ids`: https://support.airtable.com/docs/finding-airtable-ids
- `at-trouble`: https://support.airtable.com/articles/2111090641-airtable-api-common-troubleshooting

HubSpot
- `hs-contacts`: https://developers.hubspot.com/docs/api-reference/crm-contacts-v3/guide
- `hs-search`: https://developers.hubspot.com/docs/api-reference/search/guide
- `hs-props`: https://developers.hubspot.com/docs/api-reference/crm-properties-v3/guide
- `hs-ver`: https://developers.hubspot.com/docs/developer-tooling/platform/versioning
- `hs-auth`: https://developers.hubspot.com/docs/apps/developer-platform/build-apps/authentication/overview
- `hs-private`: https://developers.hubspot.com/docs/apps/legacy-apps/private-apps/overview
- `hs-test`: https://developers.hubspot.com/docs/developer-tooling/local-development/configurable-test-accounts
- `hs-spec`: https://github.com/HubSpot/HubSpot-public-api-spec-collection

Microsoft Graph
- `ms-delta`: https://learn.microsoft.com/en-us/graph/delta-query-messages
- `ms-msg`: https://learn.microsoft.com/en-us/graph/api/resources/message?view=graph-rest-1.0
- `ms-body`: https://learn.microsoft.com/en-us/graph/outlook-create-send-messages
- `ms-rows`: https://learn.microsoft.com/en-us/graph/api/tablerowcollection-add?view=graph-rest-1.0
- `ms-excel`: https://learn.microsoft.com/en-us/graph/api/resources/excel?view=graph-rest-1.0
- `ms-device`: https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code
- `ms-limits`: https://learn.microsoft.com/en-us/graph/throttling-limits
- `ms-meta`: https://github.com/microsoftgraph/msgraph-metadata

Shopify
- `sh-rest`: https://shopify.dev/docs/api/admin-rest
- `sh-orders`: https://shopify.dev/docs/api/admin-graphql/latest/queries/orders
- `sh-inv`: https://shopify.dev/docs/api/admin-graphql/latest/mutations/inventoryAdjustQuantities
- `sh-ver`: https://shopify.dev/docs/api/usage/versioning
- `sh-limits`: https://shopify.dev/docs/api/usage/limits
- `sh-dev`: https://shopify.dev/docs/apps/build/dev-dashboard/create-apps-using-dev-dashboard
- `sh-custom`: https://changelog.shopify.com/posts/legacy-custom-apps-can-t-be-created-after-january-1-2026

Slack
- `sl-post`: https://docs.slack.dev/reference/methods/chat.postMessage
- `sl-hist`: https://docs.slack.dev/reference/methods/conversations.history
- `sl-tokens`: https://docs.slack.dev/authentication/tokens
- `sl-rate`: https://docs.slack.dev/changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps
- `sl-specs`: https://github.com/slackapi/slack-api-specs

[g-creds]: https://developers.google.com/workspace/guides/create-credentials
[g-authov]: https://developers.google.com/workspace/guides/auth-overview
[g-oauth]: https://developers.google.com/identity/protocols/oauth2
[g-native]: https://developers.google.com/identity/protocols/oauth2/native-app
[g-web]: https://developers.google.com/identity/protocols/oauth2/web-server
[g-testing]: https://support.google.com/cloud/answer/15549945
[g-restricted]: https://support.google.com/cloud/answer/13465431
[sheets-rest]: https://developers.google.com/workspace/sheets/api/reference/rest
[sheets-append]: https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/append
[sheets-vio]: https://developers.google.com/workspace/sheets/api/reference/rest/v4/ValueInputOption
[sheets-uvr]: https://developers.google.com/workspace/sheets/api/reference/rest/v4/UpdateValuesResponse
[sheets-scopes]: https://developers.google.com/workspace/sheets/api/scopes
[sheets-limits]: https://developers.google.com/workspace/sheets/api/limits
[sheets-meta]: https://developers.google.com/workspace/sheets/api/guides/metadata
[gmail-list]: https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list
[gmail-msg]: https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages
[gmail-att]: https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages.attachments
[gmail-history]: https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.history/list
[gmail-sync]: https://developers.google.com/workspace/gmail/api/guides/sync
[gmail-push]: https://developers.google.com/workspace/gmail/api/guides/push
[gmail-scopes]: https://developers.google.com/workspace/gmail/api/auth/scopes
[gmail-quota]: https://developers.google.com/workspace/gmail/api/reference/quota
[docs-canvas]: https://workspaceupdates.googleblog.com/2021/05/Google-Docs-Canvas-Based-Rendering-Update.html
[notion-changelog]: https://developers.notion.com/page/changelog
[notion-ds]: https://developers.notion.com/docs/upgrade-guide-2025-09-03
[notion-query]: https://developers.notion.com/reference/query-a-data-source
[notion-filter]: https://developers.notion.com/reference/post-database-query-filter
[notion-prop]: https://developers.notion.com/reference/property-object
[notion-auth]: https://developers.notion.com/docs/authorization
[notion-limits]: https://developers.notion.com/reference/request-limits
[notion-hooks]: https://developers.notion.com/reference/webhooks-events-delivery
[at-list]: https://airtable.com/developers/web/api/list-records
[at-upsert]: https://airtable.com/developers/web/api/update-multiple-records
[at-schema]: https://airtable.com/developers/web/api/get-base-schema
[at-scopes]: https://airtable.com/developers/web/api/scopes
[at-rate]: https://airtable.com/developers/web/api/rate-limits
[at-hooks]: https://airtable.com/developers/web/api/webhooks-overview
[at-ids]: https://support.airtable.com/docs/finding-airtable-ids
[at-trouble]: https://support.airtable.com/articles/2111090641-airtable-api-common-troubleshooting
[hs-contacts]: https://developers.hubspot.com/docs/api-reference/crm-contacts-v3/guide
[hs-search]: https://developers.hubspot.com/docs/api-reference/search/guide
[hs-props]: https://developers.hubspot.com/docs/api-reference/crm-properties-v3/guide
[hs-ver]: https://developers.hubspot.com/docs/developer-tooling/platform/versioning
[hs-auth]: https://developers.hubspot.com/docs/apps/developer-platform/build-apps/authentication/overview
[hs-private]: https://developers.hubspot.com/docs/apps/legacy-apps/private-apps/overview
[hs-test]: https://developers.hubspot.com/docs/developer-tooling/local-development/configurable-test-accounts
[hs-spec]: https://github.com/HubSpot/HubSpot-public-api-spec-collection
[ms-delta]: https://learn.microsoft.com/en-us/graph/delta-query-messages
[ms-msg]: https://learn.microsoft.com/en-us/graph/api/resources/message?view=graph-rest-1.0
[ms-body]: https://learn.microsoft.com/en-us/graph/outlook-create-send-messages
[ms-rows]: https://learn.microsoft.com/en-us/graph/api/tablerowcollection-add?view=graph-rest-1.0
[ms-excel]: https://learn.microsoft.com/en-us/graph/api/resources/excel?view=graph-rest-1.0
[ms-device]: https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code
[ms-limits]: https://learn.microsoft.com/en-us/graph/throttling-limits
[ms-meta]: https://github.com/microsoftgraph/msgraph-metadata
[sh-rest]: https://shopify.dev/docs/api/admin-rest
[sh-orders]: https://shopify.dev/docs/api/admin-graphql/latest/queries/orders
[sh-inv]: https://shopify.dev/docs/api/admin-graphql/latest/mutations/inventoryAdjustQuantities
[sh-ver]: https://shopify.dev/docs/api/usage/versioning
[sh-limits]: https://shopify.dev/docs/api/usage/limits
[sh-dev]: https://shopify.dev/docs/apps/build/dev-dashboard/create-apps-using-dev-dashboard
[sh-custom]: https://changelog.shopify.com/posts/legacy-custom-apps-can-t-be-created-after-january-1-2026
[sl-post]: https://docs.slack.dev/reference/methods/chat.postMessage
[sl-hist]: https://docs.slack.dev/reference/methods/conversations.history
[sl-tokens]: https://docs.slack.dev/authentication/tokens
[sl-rate]: https://docs.slack.dev/changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps
[sl-specs]: https://github.com/slackapi/slack-api-specs
