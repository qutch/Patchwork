# Any input, one shape: design

Date: 2026-11-14. Status: draft for the owner's approval.

## Intent

The owner: "make a plan for a generalized data input change. the data should be able to come from emails, pdfs,
spreadsheets, tables, etc.. (is this feasible? if not focus on tables first)".

The owner's correction: **"it should be able to take any sort of data from an email"**. Invoices are only the demo
story. The connectors, the document model, the row checks, the prompts and the tool descriptions know nothing
about invoices or any other subject. Field names come only from the integration's spec and its destination.

This serves the approved need statement: a person hands a copy-paste chore to Patchwork from one screen. The AI
does the hard part once: it reads a messy or unfamiliar input, learns each new layout, and repairs a changed
destination. Plain code moves every record after that, and anything uncertain is held for a person.

Success:
- An email of any kind arrives from a sender Patchwork has never seen, with the data in the body, a table or a
  PDF attachment. The email is held, and LEARN learns that sender's layout once. Every row passes code checks,
  and the rows are written. Every later email from that sender is plain code.
- The same flow works on four email kinds (invoices, order and shipping confirmations, event registrations,
  support tickets), on uploaded spreadsheets, and on HTML tables in pages.

## Feasibility, per input kind

The estimates assume agents working in parallel, measured as wall-clock time with the lanes in the plan.

| Input | Verdict | How | Effort | Out of scope |
|---|---|---|---|---|
| **HTML tables and repeated blocks** (pages and HTML email bodies) | Feasible, high confidence | `<table>` comes from the teammate's `runner.documents.read_html`. Repeated blocks use a `css` row selector (selectolax, already installed). | Part of phase 1 (rows engine) | Tables built from CSS grids with no `<table>` and no repeated element class |
| **CSV / spreadsheet** (upload or attachment) | Feasible, high confidence | The teammate's `runner.documents.read_sheet` reads `.csv` and `.xlsx` (openpyxl). | Teammate (in progress), plus about 1 h of wiring in phase 1 | Formulas without cached values (held), macros, `.xls`, several header rows |
| **PDF with a text layer** | Feasible, verified | The teammate's `runner.documents.read_pdf` reads the pypdf text layer. **PDF table extraction is out of scope**, so line items are learned from the text with the `lines` row selector. I checked that pypdf returns each line item on its own text line on a generated invoice PDF. | Teammate (in progress), plus the `lines` selector in phase 1 | **Scanned or image-only PDFs** (held: "no text layer"), OCR, encrypted PDFs, line items whose description wraps onto two lines (held, because the wrapped line is unexplained) |
| **Email** | Feasible | The teammate's `runner.documents.read_eml`, and optionally `runner.imap.fetch_new` (an IMAP app-password credential, V13). A folder of `.eml` files works offline and as the stage-safe demo. | Teammate readers; about 2.5 h of connectors and wiring (phase 3) | OAuth mail APIs (Gmail/Graph), calendar and image attachments |
| **Table sources** (Google Sheets CSV URL, Airtable list records, SQLite `SELECT`) | Feasible, low demo value | Control-plane connectors. Each new row becomes one document. | ~2 h (phase 4, optional) | Writing back to the source, deletions, edits to rows already read |
| **Per-record AI** | Not done, by rule | The runner is AI-free. | — | Everything per record is plain code. |

**Verdict:** everything the owner listed is feasible except OCR. Tables come first, because the rows shape is the
core and every other input reuses it. With phase 1 alone you can demo an HTML table or an uploaded spreadsheet
becoming checked rows. With the readers and fixtures coming from the teammate, phases 1–3 plus the demo beat take about 8–9 h of critical path, which fits the 16 h left
alongside plan A. Phase 4 is the first thing to cut.

**New dependencies** (added by the teammate, all pure Python):
- `pypdf` and `openpyxl` at runtime. `pypdf` 6.10.1 is already in the local uv cache.
- `fpdf2` for dev, to make fixture PDFs.

This design adds none. The readers are called by the control plane at ingest. A compiled integration never calls
them per record: the runner reads documents that are already normalized.

### Built by the teammate (consumed here, not redefined)

These pieces are AI-free and in progress. This design and its plan only consume them:
- **`runner/documents.py`:**
  - `Table(header: list[str], rows: list[list[str]], where: str)`
  - `Document(kind: Literal["email","pdf","sheet","html","text"], name: str, text: str, html: str|None, tables:
    list[Table], meta: dict[str,str], children: list[Document])`
  - `read_eml(bytes)`, `read_pdf(bytes, name)` (text layer; `meta["no_text_layer"]="true"` when empty),
    `read_sheet(bytes, name)`, `read_html(html, url)` (`<table>` → `Table`), `read_any(bytes, name, content_type)`.
- **`runner/imap.py`** (optional): `fetch_new(host, user, credential, folder="INBOX", since_uid=0) -> list[tuple[int,
  bytes]]`.
- **`fixtures/emails/`**, generated by `scripts/make_email_fixtures.py`:
  - invoices (a body table, and a PDF attachment)
  - order and shipping confirmations with HTML item tables
  - event registrations
  - support tickets

  Each kind comes from 2 senders. There is also one `.xlsx` attachment, one image-only PDF, and a
  `<name>.expected.json` per email.

## Architecture: connectors → documents → records

```
 extension ──► page inbox (pages.db, unchanged) ───────────────► GET /api/pages ─────┐
 upload ─────┐                                                                       │  runner (AI-free subprocess)
 .eml folder ├─► runner.documents.read_any ─► document inbox (documents.db) ─► GET /api/documents ─┤  route → extract (fields + rows)
 IMAP ───────┤        (called by the control plane at ingest)                            │  → validate (incl. cross-checks)
 table srcs ─┘                                                                       │  → dedupe → map → send → verify
                                                                                     └► one Outcome per row (or per document)
```

### Calls kept from the lead, and the ones changed

- **Kept: connectors → documents → records.** Connectors run in the **control plane**, where pages are ingested
  today. Each document is normalized **once, at ingest**, with the same helpers the agents use, so everyone reads
  it the same way (the page inbox's existing rule).
  - The runner reads `patchwork://control /api/documents` exactly as it reads `/api/pages`. **No new
    `Source.kind`**, and the runner gains no PDF or IMAP code.
  - Pages and API sources behave as they do today.
- **Kept: one `rows` parser shape** for everything tabular.
- **Changed: cleaning transforms are a closed set, not `/transforms.py`.**
  - The agent picks a cleaner and its arguments (`money`, `number`, `date` with explicit formats, `split`), and
    code runs it on every sample row.
  - Why: LEARN may only write `/spec.yaml` today (`LEARN_WRITABLE`). Python written by the model and run on every
    record would need an AST allowlist and sandbox tests.
  - `custom:<fn>` from `/transforms.py` stays possible for TEACH, as today. Allowing it in LEARN is a stretch.
- **Changed (owner): the arithmetic check is now generic declared validation rules.** `sum_equals`,
  `product_equals` and `count_equals` run only when an integration's spec declares them, at learn time and at run
  time.
- **Changed: the demo writes one Airtable table, `Invoice Lines`,** with the invoice-level fields repeated on every
  line. The Airtable view is grouped by Invoice No, so each invoice shows with its lines.
  - Why: two tables need a second sink in the spec. Preflight, REPAIR, golden tests and `contract/openapi.json`
    all assume one sink contract, and V14 omits link fields (`multipleRecordLinks`).
  - An `Invoices` header table with links is a stretch (see Risks).
- **Changed: auto-deploy (D2) for documents.**
  - Pages use *corroboration* (title, og:, JSON-LD), which PDFs and emails don't have.
  - A learned document parser deploys without asking only when all of these hold: every value has evidence, the
    row checks pass, nothing is ambiguous, and **at least one declared cross-check rule passed**.
  - Otherwise a person approves.

### The document record (new contract V17)

The control plane turns the teammate's `Document` into records with one adapter, `control.documents.to_records`. A
record is flat for the runner, like the V1 page record. It adds only routing, storage and flag fields to what
`Document` carries:

```json
{"id": "doc-000031", "seq": 31, "parent_id": "doc-000030", "kind": "pdf", "integration": "invoice_lines",
 "site": "contoso-parts.example", "name": "INV-7781.pdf",
 "text": "<the PDF's text layer>", "html": "",
 "tables": [],
 "meta": {"<keys as read_eml/read_pdf set them>": "..."},
 "flags": [], "received_at": "2026-11-14T15:02:00+00:00", "received_date": "2026-11-14", "sha256": "..."}
```

- `kind`, `name`, `text`, `html`, `meta` and `children` come from `Document` unchanged.
- `tables` are its `Table`s as `{header, rows, where}`.
- `flags` is derived from the meta: `meta["no_text_layer"] == "true"` → `["no_text_layer"]`.
- `site` is the domain of the sender address in the email's meta. The exact key name is confirmed against
  `read_eml` when it lands.

- **`site`** is the routing key for every document. It holds the sender's domain for an email and its attachments,
  the URL host for a page or URL source, and `upload` for a file sent by hand. So "one parser per sender" is LEARN's
  existing "one parser per site", with no new routing code.
- **Leaves:** an email with readable attachments (PDF, CSV, XLSX, HTML, TXT) yields the attachments as leaf
  documents. Each leaf inherits the email's `meta`, so a parser can read the subject via
  `{json_path: "$.meta.subject", regex: ...}`. An email without one is its own leaf. `GET /api/documents` lists
  leaves only. Parent emails are stored for audit and the UI.
- **`flags`:** `no_text_layer` (an image-only PDF, from the teammate's meta), or `unreadable` (`read_any` raised;
  the error text goes in the detail). A flagged document is held with a plain reason (`PARSE_NO_MATCH: no text layer: a scanned or image-only PDF; Patchwork reads text PDFs only`) and
  is **never sent to LEARN**.
- **Originals** are kept at `$PATCHWORK_WORKSPACE/documents/<id>.<ext>` and never logged.

### Spec additions (new contract V18), all optional and additive

- **`Parser.rows`** selects rows and reads fields relative to each row. It may sit next to the parser's
  document-level `fields`.
  - **`select`**, exactly one of:
    - `{table: {columns: [<header names>]}}`: the first normalized table whose header contains them.
    - `{css: "<selector>"}`: each match is a row element (HTML tables or repeated blocks).
    - `{lines: {after: <regex>, before: <regex>, match: <regex with named groups>, skip: <regex>?}}`: text lines.
      This is **the PDF path**, since PDF table extraction is out of scope.
      - A line between `after` and `before` that `match` doesn't match must be explained: blank, a repeat of a line
        from before the first row (a header repeated on page 2), or matched by the declared `skip`.
      - Any other line holds the document. That is how a description wrapped onto a second line is held instead of
        being written as a short row.
  - **Row extractors:** `column` (header name), `index`, `css` (relative to the row), and `regex` (on the row's
    text, group 1).
  - **`require: [field, ...]`** (default: the first row field): a row with any of these empty is skipped. A
    skipped row must be *explained*: blank, a repeat of the header, or an aggregate (its numbers equal the sum of
    the other rows). Otherwise the document is held. Rows are never dropped silently.
  - **`hold: document | row`** (default `document`): when one row fails validation, hold every row of the document
    or just that row.
- **`FieldExtract.clean`:** `money` | `{money: {decimal: ","}}` | `number` | `{date: [<strptime formats>]}` |
  `{split: {sep, part}}` | `strip` | `upper` | `lower`. A value that fits two date formats differently is
  `VALIDATION_FAILED` ("ambiguous: 2026-03-04 or 2026-04-03"), never a guess. `json_path` may now take a refine
  `regex`.
- **Cross-check rules in `validate`:**
  - `{rule: sum_equals, rows_field: f, field: g, tolerance: 0.01}`: Σ of `f` over the rows equals `g`.
  - `{rule: product_equals, fields: [a, b], field: c, tolerance: 0.01}`: per row, a × b ≈ c.
  - `{rule: count_equals, field: g}`: the number of rows equals `g`.

  The rules use `Decimal`. A failure is `VALIDATION_FAILED` with the numbers in the detail.
- **Records:**
  - A parser with `rows` emits **one record per row**. Its fields are the document fields plus the row fields
    (the names must not collide). `record.row_no` (from 1) and `record.row_count` are available to mappings and
    dedupe keys.
  - Outcome ids are `<doc id>#<row_no>`. A document-level failure is one Outcome with the document's id.
  - Every parser of a spec has rows or none of them do.

**Example 1: invoice lines from email → Airtable** (the demo; the field names are this spec's, not the code's):

```yaml
spec_version: 1
name: invoice_lines
intent: "Copy every line of each emailed invoice into the Invoice Lines table"
source:
  kind: api
  base_url: patchwork://control
  list: {method: GET, path: /api/documents, params: {since: "{cursor}", integration: invoice_lines}}
  record_id: id
  text_field: text
  html_field: html
  cursor_field: seq
extract:
  route_by: site                       # the sender's domain
  parsers:
    - id: northwind
      match: {site: northwind-supply.example}
      origin: {kind: learned, approved_by: auto}
      fields:
        invoice_no:   {json_path: "$.meta.subject", regex: 'Invoice\s+(\S+)'}
        invoice_date: {regex: 'Date:\s*(\d{1,2}/\d{1,2}/\d{4})', clean: {date: ["%m/%d/%Y"]}}
        subtotal:     {regex: 'Subtotal\s+(\S+)', clean: money}
      rows:
        select: {table: {columns: [Description, Qty, Unit Price, Amount]}}
        fields:
          description: {column: Description}
          qty:         {column: Qty, cast: int}
          unit_price:  {column: Unit Price, clean: money}
          amount:      {column: Amount, clean: money}
validate:
  - {field: invoice_no, rule: required}
  - {rule: sum_equals, rows_field: amount, field: subtotal, tolerance: 0.01}
  - {rule: product_equals, fields: [qty, unit_price], field: amount, tolerance: 0.01}
dedupe: {key: [invoice_no, record.row_no]}
sink:
  kind: airtable
  base_id: appH3aUzXPLlI5Y2N
  table: Invoice Lines
  auth: {scheme: bearer, credential: AIRTABLE_TOKEN}
  fields:
    Invoice No: {from: invoice_no}
    Sender: {from: record.site}
    Invoice Date: {from: invoice_date}
    Line: {from: record.row_no}
    Description: {from: description}
    Qty: {from: qty}
    Unit Price: {from: unit_price}
    Amount: {from: amount}
  verify: {read_back: true}
```

**Example 2: a spreadsheet attached to shipping notices → SQLite** (the second beat):

```yaml
name: shipment_items
intent: "Record every item of each shipping notice in the shipment items table"
source: {kind: api, base_url: patchwork://control, list: {method: GET, path: /api/documents,
         params: {since: "{cursor}", integration: shipment_items}}, record_id: id, text_field: text, cursor_field: seq}
extract:
  route_by: site
  parsers:
    - id: parcelhub
      match: {site: parcelhub.example}
      fields:
        reference: {json_path: "$.meta.subject", regex: 'Shipment\s+(\S+)'}
      rows:
        select: {table: {columns: [SKU, Description, Qty Shipped]}}
        hold: row
        fields:
          sku:         {column: SKU}
          description: {column: Description}
          qty:         {column: Qty Shipped, cast: int}
validate:
  - {field: qty, rule: positive_int}
dedupe: {key: [reference, sku]}
sink:
  kind: sqlite
  path: shipments.db
  table: shipment_items
  fields: {Reference: {from: reference}, SKU: {from: sku}, Description: {from: description}, Qty: {from: qty}}
```

A support-ticket spec has `fields` only, with no `rows`, and writes one record per email, exactly as a page does
today.

## The deep agent's role

All AI goes through the one deep agent. There is **no new mode**:
- LEARN learns a new sender's document.
- TEACH-from-examples (plan A) also accepts documents as examples.
- REPAIR is unchanged.

The tools are today's tools, made document-aware, with every guardrail in code (`agents/learn.py`):

| Tool | What changes | Code checks (a failure is `REFUSED`/`FAILED` with the reason) |
|---|---|---|
| `get_page(page_id)` | For a `doc-` id it returns a document outline: metadata, numbered text lines (capped), each table's header, its row count and its first 3 rows, and the HTML outline when there is HTML. | — |
| `test_page_parser(parser)` | Runs the document fields **and** `rows`, then shows raw → cleaned values per row. | **Evidence:** each document value occurs in the text, a table cell or the metadata. **Row checks:** each row value occurs in *its own* row's text; required fields are present in at least 95% of the rows; no two rows are identical; every skipped row is explained (blank, header repeat, aggregate); the row count equals the selected region's data rows. **Declared rules** run, including the cross-checks. A regex that is ambiguous (several different matches) or captures a literal value is flagged. |
| `save_parser(parser, reason)` | Accepts `rows`, and seeds a golden example with `expect: {rows: [...]}`. | Every check above, plus the existing ones. D2 for documents: auto only when every check passed **and** a declared cross-check passed; otherwise `request_deploy`. |
| `propose_mapping` (plan A) | For document examples it takes an optional `parser` (document fields + rows) for the first example. Mapping entries may name its fields with `{"field": f}`. | The same shared check helper as `save_parser`. |
| `add_site_parser` (plan A) | Accepts `rows`. | The same shared check helper. |

- **Smart input happens at learn time.** The agent reads a messy document once. It decides which table or lines
  are the rows, maps column headers to the integration's fields (`Rate` → `unit_price`), picks cleaners
  (`$1,299.00`, `(12.50)`, `03 Nov 2026`) and pins one date format per sender.
- **Prompts:** `prompts.document_learner_prompt(spec)` is domain-free. It names only the integration's intent,
  fields and rules.
- **The scripted agent** (stage fallback) emits the same events:
  - `propose_rows_parser(doc, spec)` picks the table whose header best matches the integration's field names or
    the existing parsers' column names.
  - It sets `clean: money` on money-looking columns.
  - It reads document-level fields from `Label: value` lines whose labels match the field names or the existing
    parsers' regex labels.
- **Budget:** LEARN on a document takes 6 model calls or fewer, about 12 s on Haiku (`PATCHWORK_LEARN_MAX_CALLS`
  stays 12).
- **UI labels:** every learned value carries the existing `evidence` event, which gains `rows: {count, checks:
  [{name, ok, detail}]}`. The UI labels the parser "learned by AI ✓ checked" and shows each cross-check's numbers.

## Plugging into plan A's wizard

- The wizard gains step **"Where does the data come from?"** between *What* and *Where it goes*. The options are:
  - Web pages: plan A's path, unchanged.
  - Email: a folder of `.eml` files, or an IMAP mailbox with an app-password credential.
  - Files: upload PDF, CSV or XLSX.
  - A table: phase 4.
- Step 3 ("Examples") lists the documents that have arrived for the draft, as well as pages.
- Plan A's `write_teach_inputs(name, page_ids)` accepts `doc-` ids too.
- Nothing in plan A is duplicated. The plan names the shared files.

## Demo beats

**Beat 1 (30 s): invoices → Airtable.** The `invoice_lines` integration is live with the teammate's first invoice
sender (a table in the email body). The Airtable view is grouped by Invoice No.

1. (0–5 s) The presenter clicks **Deliver** on the second invoice sender's email, a PDF attachment from
   `fixtures/emails/`. It appears under the integration within about 2 s.
2. (5–8 s) The run holds it: "no parser for site=<sender domain>". **Needs you** shows one held document. LEARN
   starts by itself.
3. (8–20 s) The Activity panel streams the steps:
   - get_page shows the PDF's text lines, numbered.
   - test_page_parser runs a `lines` selector (between the column-heading line and the first total line) plus money
     cleaners. Every row value is found in its own line, and every other line is explained.
   - **Σ line totals = the subtotal ✓**, and quantity × unit price = line total on every row ✓.
   - save_parser deploys the new version automatically, because every check passed.
4. (20–30 s) The held invoice is replayed. Its lines appear in Airtable under its invoice number, matching its
   `.expected.json`.

   Presenter line: "The AI read that layout once. From now on this sender's invoices are plain code, and if the
   lines ever don't add up, the invoice is held, not written."

**Optional 10 s:** Deliver a copy of the body-table sender's invoice with one line amount changed (the demo
endpoint makes this variant by editing the HTML). It is held with "sum_equals: rows sum to … but subtotal is …", and nothing is written.

**Beat 2 (test, or a 15 s beat if time allows):** the same flow on a different kind.
- Order or shipping confirmations go to SQLite `shipment_items`.
- The first sender is live. The second sender's HTML item table is learned once, then written.
- `python -m demo_apps.sqlite_tracker --rows` shows the rows.
- The offline test covers all four kinds.

## Risks

1. **Two-table write (header + linked lines).**
   - Not in the critical path: it needs a second sink contract, Airtable link fields and partial-write handling.
   - Fallback (the plan): one table with the header fields repeated.
   - Stretch: `sink.lines`, with every body validated before any write, and parts deduped per key so a replay
     never writes the header twice.
2. **PDF line items from text only** (no PDF table extraction).
   - It depends on pypdf putting each line item on one text line, which I checked on a generated PDF. Real PDFs may
     split or merge columns differently.
   - A repeated header on page 2 is an explained skip. A wrapped description is an unexplained line, so the
     document is held, not written.
   - Without declared cross-checks the safety net is weaker: a person approves the learned parser.
   - **Rehearse on 2–3 real PDFs before the demo.**
3. **LEARN on noise.** Examples: a terms-and-conditions PDF next to the invoice, a newsletter in the watched
   folder.
   - Its fields fail evidence, so it stays held.
   - Cost: one LEARN's model calls.
   - Mitigation: connector filters (`from_domains`, `subject_regex`, `attachment_glob`) and the existing rule of one
     learn per document.
4. **Ambiguous values.** `03/04/2026`, `1.299,00`. These are pinned per sender at learn time. If the sample itself
   is ambiguous, `ask_user`; at run time the value is held.
5. **Pages vs documents regressions.**
   - `control.autorun.is_page_spec` is true for any `patchwork://control` source today, so it must exclude
     `/api/documents`.
   - Page hooks must use the right inbox.
   - Covered by tests in G4.
6. **Merge conflicts with plan A.** Both edit `agents/deep_tools.py`, `agents/scripted_v2.py`, `agents/prompts.py`,
   `control/api/server.py`, `control/autorun.py` and the wizard. The plan marks every shared file. The rows changes
   go into one extracted check helper that plan A Task 5 also calls.
7. **Live IMAP on stage.** Network, 2FA and Gmail limits. The `.eml` folder is the stage path, and IMAP is only
   rehearsed.
8. **A corrected re-issue of a document with the same key** is a `duplicate` and is not written. That is safe, but a
   person may not notice it. The tracker shows duplicates.

## Out of scope

OCR and scanned PDFs (held), PDF table extraction (line items come from text), per-record AI, OAuth mail APIs,
`.xls`, writing back to sources, two-table writes (a stretch), and custom Python cleaners in LEARN (a stretch).

## Testing

- **Offline, with fixtures, fakes and in-process servers.** Free ports only, never 8000–8003, 5180 or 38091.
  Scripted agent and fake chat models.
- **Fixtures:** the teammate's `fixtures/emails/`, with 4 kinds × 2 senders, the `.xlsx` attachment and the
  image-only PDF. Each email's `<name>.expected.json` is the oracle the end-to-end tests compare against.
  - The tests make two variants in code: a bad-sum copy of one invoice, and a terms-PDF attachment added to one
    (`fpdf2`, dev).
- **No domain words:** a test asserts that the input layer contains none of them:
  - `runner/documents.py` and `runner/imap.py` (the teammate's)
  - `control/connectors/`, `control/documents.py`, `runner/rows.py`, `runner/clean.py`
  - the document prompt and the document tool descriptions.
- **Regression:**
  - v1 `po_to_stockroom` and v2 jobs are unchanged.
  - The compiled v1 and jobs packages contain no rows code.
  - All existing tests and smokes stay green.
