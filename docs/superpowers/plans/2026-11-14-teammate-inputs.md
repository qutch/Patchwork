# Teammate plan: reading any document into Patchwork

Owner: the human teammate. The lead (Claude, with agents) merges your PRs and owns everything not listed here.

**Goal:** Patchwork can take data from **emails, PDFs, spreadsheets and tables**, about any subject. You build the
AI-free part that *reads* those inputs: files become one normalized `Document`, plus the test data that proves it,
plus the connectors that fetch new rows from tables. The lead's agents build the rest: the `rows` parser, the
pipeline, the document inbox, and the deep agent learning each new sender's layout.

**Read first:**
- `docs/superpowers/specs/2026-11-14-generalized-input-design.md`: the whole design, and the "Built by the
  teammate" section, which is your contract.
- `docs/superpowers/plans/2026-11-14-generalized-input.md`: the full plan. Your tasks there are marked "teammate";
  this file orders them for you.
- `docs/contracts.md` V13 (credentials) and V16 (your SQLite sink, as a style reference).

## Decisions (2026-11-14)

Only decision 4 came from the owner. Decisions 1, 2, 3 and 5 are the lead's recommended defaults, **not yet
confirmed by the owner**. Build to them, but change course if the owner overrules one.

1. *(Default, to confirm)* **One "Invoice Lines" table** (invoice fields repeated on each line, grouped by invoice number) is fine for the demo.
2. *(Default, to confirm)* **Layouts with no total to check** (support tickets, event registrations) always need a person's approval before
   they go live. Only layouts with a passing declared check (e.g. line items sum to the total) deploy on their own.
3. *(Default, to confirm)* **A folder of `.eml` files is enough for the demo.** Live email (task 3) stays optional and comes after tasks 6-7.
4. *(Owner)* **Table sources are lowest priority** (task 5, cut first).
5. *(Default, to confirm)* **2-3 real invoice PDFs are OK for rehearsal only.** Keep them outside the repo (never commit real personal or
   company data) and put made-up look-alikes in `fixtures/`.

## Update (2026-11-14, evening): G4–G7 are yours now

The owner moved G4–G7 from the lead's agents to you (and your agents). The task text is in
`docs/superpowers/plans/2026-11-14-generalized-input.md` (Task G4 onwards); read its Global Constraints and Review
Focus first. Lane G (G1–G3) and the whole New integration wizard (plan A lanes 1–3) are on `main`.

**Ownership change.** For G4–G7 you may edit the files those tasks name, including `control/*.py`, `control/api/`,
`agents/` and `runner/pipeline.py`. The "Your files" list under Rules below still applies to tasks 1–7 of this
file. `web/` and plan A Task 9 (`scripts/smoke_v2.py`, `fixtures/events/`, `fixtures/papers/`,
`demo_apps/fake_papers_api/`) stay with the lead.

**What is already built, and what to reuse:**
- `runner/pipeline.py` `process_document(integ, record, http, store)`: one document → many rows. Every row is
  validated and its body built before the first send; a failed declared check (`sum_equals`, …) holds the whole
  document (`VALIDATION_FAILED`, both numbers in the detail) and writes nothing. Don't write a second one.
- `runner/rows.py` reads tables from `record["tables"]` when present (a list of `{header, rows, where}`), else from
  `record["html"]` via `runner.documents.read_html`. Two G2 tests skip until `runner/documents.py` exists.
- `runner/clean.py`: the cleaners (`money`, `number`, `date`, …). Ambiguous values are refused, never guessed.
- `docs/contracts.md` V18 (rows, `clean`, cross-checks, per-row records) is the last section of the file: put V17
  (the document record and inbox) above it.

**Fixed names (the code already depends on them):**
- Document flags on a record: `no_text_layer`, `encrypted`, `unreadable`. `runner/documents.py` and G4 must use
  exactly these. For `unreadable`, the error text is read from `meta["error"]`.
- The sender address is `meta["from"]` (bare, lower-cased), as Task 2 above says; G4 routes by its domain.

**Known gaps to handle in G4–G6:**
- **Per-record vs per-document.** `store.latest_outcomes` (the tracker and UI) still keys on record id, so one
  document can show as held (`doc-1`) and as written rows (`doc-1#2`). Re-running held documents already groups by
  document (`quarantined_raw(name, id_field=RECORD_ID)`); make G4's inbox and the outcomes it reports do the same.
- **Footer rows in tables.** A table select has no declared `skip`, so a footer row that is not a sum ("Tax",
  "Shipping") holds every such document. Safe, but LEARN (G5–G6) must know: propose a `skip` pattern or a `require`
  on a numeric column that footers leave empty.
- **Rows that look like a total are held.** A kept row whose numeric cells equal the sum of the other rows holds
  the document, even when it is real data (e.g. quantities 1, 1, 2). Expect some holds on single-number tables.
- **`lines` selects without `after`.** Any text above the first item must be blank or matched by `skip`, or the
  document is held. Give LEARN's line readers an `after` anchor (the column-heading line).
- **`number` vs `money`.** Only `money` refuses an ambiguous `1.299`; `number` follows the declared decimal mark.
- **`json_path` + `regex`** is now a valid refine (the regex runs on the JSON value). Agent code that treated a
  regex next to `json_path` as a primary text regex may need checking.
- **Runs of one integration never overlap** (`runner/run.py` `run_lock`). A run waits for the other; don't start
  two runs of one integration from a test and expect them to interleave.

**Overlap with the lead.** Plan A Task 9 may make small changes to `agents/scripted_v2.py` or `agents/learn.py`,
which G5–G6 also touch. Pull `main` before you start on those two files; the lead pushes Task 9 as soon as its
review passes.

## Rules (same as the rest of the project)

- **No AI, no network:** everything you write is plain code (`runner/` is AI-free and must stay that way). No
  network in tests: in-process servers on free ports, and mocks for IMAP.
- **Ports:** never bind or kill 8000–8003, 5180 or 38091.
- **Secrets:** never commit secrets or real personal data. Fixtures use made-up names, companies, emails and
  addresses.
- **Your files:**
  - `runner/documents.py`, `runner/imap.py`
  - `fixtures/emails/`, `scripts/make_email_fixtures.py`
  - `control/connectors/` (task 5)
  - `tests/inputs/`, plus the tests named in tasks 6–7
  - `pyproject.toml` (dependencies only)

  The agents are working in `agents/`, `control/api/`, `control/*.py`, `compiler/`, `spec/`, `runner/rows.py`,
  `runner/clean.py`, `runner/pipeline.py` and `web/`. If you need a change there, ask the lead instead of editing.
- **No domain words:** no subject-specific code. Nothing in `runner/documents.py` or the connectors may know
  about invoices, orders or tickets. The lead adds a test that greps for those words.
- **PRs:** one PR per task to `main`. Run the full suite (`.venv/bin/python -m pytest -q`, about 5 min) before
  each one. The lead merges.

## Task 1: test data, many kinds of email (~1–1½ h). Start now.

- **Files:** `scripts/make_email_fixtures.py` creates `fixtures/emails/`. Commit both the script and its
  output.
- **Content:** 2 senders per kind, with clearly different layouts:

| Kind | Where the data is | Rows? |
|---|---|---|
| Invoice | in the body (an HTML table) for sender A; a **text PDF attachment** for sender B | line items |
| Order / shipping confirmation | an HTML item table in the body | items |
| Event registration | body text | no |
| Support ticket | body text (priority, category, ticket id) | no |

- **Extras:** one email with an **`.xlsx` attachment** (rows), and one with an **image-only PDF** (no text layer,
  which must be held later).
- **Answer files:** each email gets a `<name>.expected.json`:
  `{"kind": "...", "sender": "...", "fields": {...}, "rows": [{...}, ...]}`. Later tasks use this file as the
  answer key, so it has to be exactly what a correct read produces.
- **Dependencies:** add `fpdf2` and `openpyxl` to the `dev` extra in `pyproject.toml`; `openpyxl` also goes in
  runtime in task 2.
- **Done when:** running the script twice gives the same files byte for byte (fixed dates, no randomness), and a
  test in `tests/inputs/test_fixtures.py` checks that every `.eml` has an `.expected.json`.

## Task 2: `runner/documents.py`, any file → one `Document` (~2–3 h)

This is the contract the lead's plan builds on. Keep these names and types exactly:

```python
@dataclass
class Table:
    header: list[str]
    rows: list[list[str]]
    where: str                  # "html table 1", "sheet Orders", "page 2"

@dataclass
class Document:
    kind: Literal["email", "pdf", "sheet", "html", "text"]
    name: str                   # subject, file name or url
    text: str                   # plain text, whitespace-normalised, line breaks kept between lines
    html: str | None
    tables: list[Table]
    meta: dict[str, str]        # from, to, subject, date, url, content_type, no_text_layer="true", …
    children: list["Document"]  # attachments

def read_eml(data: bytes) -> Document
def read_pdf(data: bytes, name: str = "") -> Document
def read_sheet(data: bytes, name: str) -> Document
def read_html(html: str, url: str = "") -> Document
def read_any(data: bytes, name: str, content_type: str | None = None) -> Document
```

- **`read_eml`** (stdlib `email`):
  - `meta["from"]` is the bare address, lower-cased. The lead's plan routes by the sender's domain, so tell the
    lead the exact meta key you use.
  - The body: the HTML part goes to `html`, and `text` comes from the HTML when there is no plain part.
  - Tables come from HTML bodies. Every attachment becomes a child `Document` via `read_any`.
- **`read_pdf`** (`pypdf`, runtime dependency):
  - The text layer, one line per text line, in page order. The lead's line-item reader depends on lines staying
    lines.
  - An empty text layer sets `meta["no_text_layer"] = "true"`.
  - Pulling tables out of PDFs is out of scope.
- **`read_sheet`:** `.csv` (stdlib `csv`, sniff the delimiter) or `.xlsx` (`openpyxl`, read-only, values not
  formulas). One `Table` per sheet; dates come out as ISO strings.
- **`read_html`:** use `selectolax`, already a dependency.
  - Each `<table>` becomes a `Table`. The header comes from `<th>`, or from the first row when there is no
    `<th>`.
  - Empty spacer rows are dropped, but a "Total" row is kept: the lead's code excludes it.
- **`read_any`:** dispatches by content type, then by extension. An unknown type gives `kind="text"` with the
  decoded text.
- **Tests:** `tests/inputs/test_documents.py`, over every task 1 email:
  - every expected field value appears in the right document's `text`;
  - expected rows match the right `Table` (or, for the PDF, appear as lines);
  - attachments are children;
  - the image-only PDF is flagged;
  - an empty or broken file raises a clear `ValueError` naming the file, never a crash deep inside a library.

## Task 3 (optional): `runner/imap.py`, read new mail (~1 h)

- **The function:** `fetch_new(host: str, user: str, credential: str, folder: str = "INBOX", since_uid: int = 0)
  -> list[tuple[int, bytes]]`.
  - It is read-only: `select(folder, readonly=True)`, never flag, move or delete.
  - The password is `runner.credentials.get(credential)` (e.g. an app password named `IMAP_PASSWORD`).
  - It returns `(uid, raw .eml bytes)` for UIDs above `since_uid`, oldest first.
- **Errors:** a login failure raises an error whose message names the credential, never its value
  (`runner.credentials.redact`).
- **Tests:** mock `imaplib.IMAP4_SSL` only. No real mailbox.

## Task 4: answer the lead's questions about your readers (~15 min, after task 2)

When task 2 is merged, send the lead:
- the meta key for the sender address;
- how line breaks look in PDF text;
- anything in the contract you had to change.

The agents' tasks G4, G8 and G9 build on these details.

## Task 5: table connectors, new rows from a sheet or table (~2 h). Plan task G13.

**Lowest priority, cut first.** The owner decided (2026-11-14) that table sources matter least for the demo. Do this
only after tasks 1-4, 6 and 7 are done, and only if time remains. Nothing else depends on it.

- **Files:** `control/connectors/csv_url.py`, `airtable_list.py` and `sqlite_select.py`.
- **What `poll()` yields:** each connector's `poll()` yields **one item per new row** as a one-row CSV
  (`text/csv`) plus `meta.row_key` and `site`:
  - `meta.row_key` is the Airtable record id, the SQLite rowid, or the sha256 of the CSV row's cells;
  - `site` is the source's host, or `sqlite`.

  Seen row keys are kept in connector state, so a re-poll yields only new rows.
- **Per connector:**
  - `CsvUrl(url)` reads a Google Sheets "publish to CSV" URL.
  - `AirtableList(base_id, table, credential)` pages through `offset`. Reuse `runner/airtable.py`'s request
    helpers, and never print the token.
  - `SqliteSelect(path, query)` accepts exactly one `SELECT`, opened read-only (`mode=ro`). Copy your own V16
    path-safety rules.
- **Tests:** `tests/control/test_table_connectors.py`:
  - a CSV server on a free port with 3 rows gives 3 items, and appending a row gives 1 more on the next poll;
  - SQLite `DELETE FROM t` is refused;
  - fake Airtable (`demo_apps/fake_airtable`, `pageSize=2`) gives every record once.
- **Note:** the lead's G4 (the document inbox) is what calls `poll()`. Build against the interface above; the lead
  wires it in.

## Task 6: PDF line items through the pipeline (~30 min, after the agents' G2–G4 land). Plan task G8.

- **Test:** write `tests/engine/test_pdf_documents.py` from G8 in the full plan. Text PDFs made in the test with
  `fpdf2` go through `read_pdf` → `to_records` → `process_document`.
  - 3 line items become 3 rows with the right amounts.
  - An image-only PDF is held with "no text layer".
- **If the rows engine can't express a layout** (e.g. one row split over two text lines), report it to the lead.
  Don't change `runner/rows.py`.

## Task 7: every fixture email end to end (~45 min, after G4 lands). Plan task G9.

- **Test:** `tests/control/test_email_documents.py`, over every `fixtures/emails/*.eml`. Each has leaves with a
  sender site, attachments become their own leaves, and the image-only PDF is flagged.
- **Result:** your `expected.json` files become the answer key for the lead's G11, the four-kinds end-to-end test.

## Order and timing

1. **Now:** task 1, then task 2. These unblock the most work: G4, G8, G9 and the demo.
2. **Then:** nothing optional before tasks 6 and 7. Task 3 (live email) only after them, if the owner asks.
3. **When the lead says G2–G4 have landed:** tasks 6 and 7.
4. **Last, optional:** task 5. It is the first thing to cut.
5. **After each task:** a PR to `main`; the lead merges, usually within 30 minutes.
