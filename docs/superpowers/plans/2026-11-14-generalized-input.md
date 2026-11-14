# Generalized Input (Any Data From Email, PDF, Spreadsheet, Table): Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Any data that arrives as an HTML table, a spreadsheet, a text PDF or an email (any kind, any sender) becomes
checked records. One `rows` parser shape covers them all, and the deep agent learns each new sender's layout once.

**Architecture:**
- **Ingest:** connectors in the control plane (upload, a folder of `.eml` files, IMAP, table sources) normalize every
  input once, at ingest, into a *document*: text, html, tables, meta and flags. Documents are stored in a document
  inbox. Email attachments become leaf documents.
- **Runner:** it reads `patchwork://control /api/documents` exactly as it reads `/api/pages`. A parser may have
  `rows`, and then one document becomes many records. Every check, including the declared cross-check rules, is plain
  code.
- **Agents:** LEARN and plan A's TEACH-from-examples accept documents. They use the same tools, with rows checks in
  code. The scripted agent mirrors them.

**Tech Stack:** Python 3.12, FastAPI, selectolax, deepagents 0.7.38, React + TypeScript (`web/`). The teammate supplies
`runner/documents.py` (pypdf, openpyxl), `runner/imap.py` and `fixtures/emails/` (fpdf2, dev).

**Spec:** `docs/superpowers/specs/2026-11-14-generalized-input-design.md`. Read it with this plan. Plan A
(`docs/superpowers/plans/2026-11-14-new-integration-wizard-a.md`) and `docs/contracts.md` V1–V16 define every existing
interface named here.

## Global Constraints

- **Agents:** all AI goes through the one deep agent. New behaviour is a mode or tools of it, and guardrails live in tools (code checks), never only in prompts. The scripted agent gets matching scripts emitting the same events.
- **Runner:** stays AI-free (subprocess, stripped env, no model keys). Everything that runs per record is plain code. No per-record AI judgment fields.
- **Records:** never silently write a wrong row; when in doubt, hold the record for a person. Every AI-produced value is checked by code and labelled in the UI.
- **Regression:** v1 (po_to_stockroom) and the v2 jobs flow keep working; all existing tests stay green.
- **Tests:** offline, using fixtures, fakes and in-process servers. Ports 8000–8003, 5180 and 38091 are never used. Model budget is tight, so prefer the scripted agent and fake models in tests.
- **Dependencies:** pure-Python and already installable with uv (check pyproject.toml). Name any you add (e.g. pypdf, openpyxl) and say why.
  - This plan adds none. The teammate adds `pypdf` and `openpyxl` (runtime) and `fpdf2` (dev).
  - The readers are called by the control plane at ingest, never per record by a compiled integration.
- **Domain-free (owner):** the input layer must not mention any subject: `runner/documents.py`, `runner/imap.py`,
  `control/connectors/`, `control/documents.py`, `runner/rows.py`, `runner/clean.py`, the document prompt, and the
  document tool descriptions. Fields come only from the spec. Fixtures and demo docs may use subject words.
- **Teammate's pieces are consumed, not redefined (in progress):** `runner/documents.py`, `runner/imap.py` and
  `fixtures/emails/` (from `scripts/make_email_fixtures.py`, with a `<name>.expected.json` per email). Their
  interfaces are in the design doc, under "Built by the teammate". No task here edits them. If one is missing an
  interface you need, ask the teammate.
- **Contracts:** each task that changes a contract updates `docs/contracts.md` in the same commit:
  - V17: the document record and the document inbox.
  - V18: rows, `clean`, the cross-check rules, and per-row records.
  - V19: connectors.
- **Git:**
  - Commit on `project-roadmap-mvp-68d423de` (or merge worktree branches into it).
  - Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
  - Python is `.venv/bin/python`.

## Review Focus

1. **A footer row inside the table body, or a stray line between PDF line items.**
   - Examples: the table's last row reads "Sum … 4.00" with the first column empty; the column-heading line repeats
     on page 2 of a PDF; a description wraps onto a second text line.
   - Expect a footer or a repeated heading to be skipped *and explained*.
   - A skipped row or an unmatched line that is not blank, not a repeated heading, not an aggregate and not matched
     by the declared `skip` holds the document. Rows are never silently dropped.
   - Tested in G2 and G8.
2. **A declared cross-check fails at run time:** the lines sum to 3,205.00 but the declared total is 3,215.00. Expect
   one held Outcome for the document (`VALIDATION_FAILED`, with both numbers in the detail), and **no** row written.
   Tested in G3.
3. **Ambiguous values:** `03/04/2026` with two allowed formats, and `1.299,00` with no `decimal: ","`. Expect
   `VALIDATION_FAILED` that names both readings, never a guess. Tested in G2.
4. **The same email delivered twice, a CSV re-uploaded, or a held document replayed after a partial success.**
   Expect no row written twice: dedupe per row, and a replay re-processes the document once. Tested in G3 and G10.
5. **An email with an image-only PDF, or with an invoice PDF next to a terms PDF.**
   - The image-only PDF is held with "no text layer" and never sent to LEARN.
   - The terms PDF is held (`PARSE_NO_MATCH`) and never written.
   - The invoice is written.

   Tested in G8 and G11.

---

## Phase 1: tables (demoable on its own: an HTML table page or a CSV/XLSX upload becomes checked rows)

### Task G1: Spec: `rows`, `clean`, cross-check rules (V18)

**Files:**
- Modify: `spec/models.py`, `docs/contracts.md` (add V18)
- Test: `tests/engine/test_spec_rows.py`

**Interfaces:**
- Produces:
  - `spec.models.RowExtract(_Strict)`: one of `column: str | None`, `index: int | None`, `css: str | None` or `regex:
    str | None`, plus `attr`, `cast` and `clean`. A `regex` next to `column`, `index` or `css` is a refine.
  - `spec.models.RowSelect(_Strict)`: exactly one of `table: TableSelect | None` (`TableSelect.columns: list[str]`,
    which must not be empty), `css: str | None`, or `lines: LineSelect | None` (`after: str | None`, `before: str |
    None`, `match: str`, `skip: str | None`). `match`'s named groups are the row's cells, which `column` reads by name
    (case-insensitive), just as it reads a table header.
  - `spec.models.Rows(_Strict)`: `select: RowSelect`, `fields: dict[str, RowExtract]` (not empty, idents),
    `require: list[str] | None` (subset of `fields`; default `[first field]`) and `hold: Literal["document","row"] =
    "document"`.
  - `Parser.rows: Rows | None = None`. `Parser.all_fields() -> set[str]` returns the field names plus the row field
    names. A name used in both is an error.
  - `FieldExtract.clean` and `RowExtract.clean`: `str | dict | None`, checked against `runner.clean.CLEANERS` (`money`,
    `number`, `date`, `split`, `strip`, `upper`, `lower`). `date` needs a non-empty list of formats.
  - `FieldExtract`: a `regex` next to `json_path` is now a refine.
  - `ValidationRule.rule` gains `"sum_equals"`, `"product_equals"` and `"count_equals"`, with the new optional
    attributes `rows_field: str | None`, `fields: list[str] | None` and `tolerance: float = 0.01`. The rules need,
    respectively: `rows_field` + `field`; `fields` (2) + `field`; and `field`. All three are allowed only when every
    parser has `rows`.
  - `Spec._references`:
    - Common fields are the intersection of `all_fields()`.
    - `record.row_no` and `record.row_count` are valid refs.
    - Either every parser has `rows` or none does.
    - `rows_field` and `fields` must name row fields.

- [ ] **Step 1: Write the failing tests**

```python
def test_invoice_example_from_design_parses():
    spec = Spec.model_validate(yload(DESIGN_EXAMPLE_1))  # copied from the design doc into the test
    p = spec.extract.parsers[0]
    assert p.rows.select.table.columns == ["Description", "Qty", "Unit Price", "Amount"]
    assert p.all_fields() == {"invoice_no", "invoice_date", "subtotal", "description", "qty", "unit_price", "amount"}
    assert p.rows.require == ["description"] and p.rows.hold == "document"

def test_mixed_rows_and_no_rows_parsers_rejected():
    with pytest.raises(ValidationError, match="every parser has rows or none"):
        Spec.model_validate(with_second_parser_without_rows(DESIGN_EXAMPLE_1))

def test_cross_check_needs_rows_fields():
    bad = replace_rule(DESIGN_EXAMPLE_1, {"rule": "sum_equals", "rows_field": "subtotal", "field": "subtotal"})
    with pytest.raises(ValidationError, match="rows_field 'subtotal' is not a row field"):
        Spec.model_validate(bad)

def test_unknown_cleaner_and_dateless_date_rejected():
    for clean in ("guess", {"date": []}):
        with pytest.raises(ValidationError):
            FieldExtract(regex="(x)", clean=clean)

def test_existing_specs_still_validate():
    for p in ("fixtures/spec.yaml", "fixtures/jobs/spec.yaml", "fixtures/products/spec.yaml"):
        Spec.model_validate(yload(Path(p).read_text()))
```

- [ ] **Step 2: Run and confirm they fail.** Run `.venv/bin/python -m pytest tests/engine/test_spec_rows.py -q`.
  Expected: FAIL (`Parser` has no `rows`).
- [ ] **Step 3: Implement** in `spec/models.py`. `runner.clean.CLEANERS` is a tuple of names. Define it in G2's file
  now as a one-line module, so the validator can import it.
- [ ] **Step 4: Run the tests and confirm they pass.** Run
  `.venv/bin/python -m pytest tests/engine/test_spec_rows.py tests/test_spec.py tests/engine/test_spec_v2.py -q`.
  Expected: PASS.
- [ ] **Step 5: Commit** with message `Spec: rows parser shape, clean, cross-check rules (contracts V18)`.

### Task G2: The rows engine and cleaners (one implementation for the runner and the agents)

**Files:**
- Create: `runner/rows.py`, `runner/clean.py`
- Modify: `docs/contracts.md` (V18 additions)
- Test: `tests/engine/test_rows.py`, `tests/engine/test_clean.py`

**Interfaces:**
- Consumes:
  - G1's `Rows` shape, as plain dicts. The engine takes dicts, so agents can test candidates without building a
    pydantic model.
  - The teammate's `runner.documents.read_html(html, url) -> Document` (in progress). Its `tables` hold
    `Table(header, rows, where)`. No `<table>` parsing is written here.
- Produces:
  - `runner.clean.clean_value(value: Any, spec: str | dict, where: str) -> Any`: raises `Quarantine("VALIDATION_FAILED",
    ...)`.
  - `runner.rows.Row`, a dataclass: `no: int`, `text: str`, `cells: dict[str, str]`, `fields: dict[str, Any]`.
  - `runner.rows.select_rows(record: dict, select: dict, html_field="html", text_field="text") -> list[tuple[str,
    dict[str, str] | Any]]`: returns each raw row's text plus its cells (table), its element (css) or its match
    groups (lines).
  - `runner.rows.extract_rows(record: dict, rows: dict, where: str, html_field="html", text_field="text") ->
    list[Row]`:
    - The tables come from `record["tables"]` (V17 dicts) when present. Otherwise they come from
      `read_html(record[html_field], record.get("url", "")).tables`, as dicts (a page record).
    - For `lines`, a line between `after` and `before` that `match` misses must be blank, a repeat of a line before
      the first row, or matched by `skip`. Otherwise `PARSE_NO_MATCH "<where>: line '<text>' is not a row and not
      explained"`.
    - Rows missing a `require` field are skipped.
    - Every skipped row must pass `explain_skip`, else `Quarantine("PARSE_NO_MATCH", "<where>: row N skipped but not
      blank, a header or an aggregate: '<text>'")`.
    - Zero rows → `PARSE_NO_MATCH`.
  - `runner.rows.explain_skip(row_text: str, cells: list[str], header: list[str], kept: list[list[str]]) -> str |
    None`: returns `"blank"`, `"header"`, `"aggregate"` (every numeric cell equals the sum of that column over the
    kept rows, within 0.01) or `None`.
  - `runner.rows.check_cross(rule: dict, doc_fields: dict, rows: list[Row]) -> str | None`: `sum_equals`,
    `product_equals` and `count_equals` with `Decimal`. Returns `None` on success, else a detail with both numbers,
    formatted with thousands separators.

- [ ] **Step 1: Write the failing tests**

```python
TABLE = ("<table><tr><th>Item</th><th>Qty</th><th>Each</th><th>Line</th></tr>"
         "<tr><td>Bolt</td><td>2</td><td>$1.50</td><td>$3.00</td></tr>"
         "<tr><td>Nut</td><td>4</td><td>$0.25</td><td>$1.00</td></tr>"
         "<tr><td></td><td></td><td>Sum</td><td>$4.00</td></tr></table>")
ROWS = {"select": {"table": {"columns": ["Item", "Qty"]}},
        "fields": {"item": {"column": "Item"}, "qty": {"column": "Qty", "cast": "int"}, "line": {"column": "Line", "clean": "money"}}}

def test_html_table_rows_skip_and_explain_footer():
    rows = extract_rows({"id": "d1", "html": TABLE, "text": visible_text(TABLE)}, ROWS, "p.rows")
    assert [(r.no, r.fields) for r in rows] == [(1, {"item": "Bolt", "qty": 2, "line": 3.0}), (2, {"item": "Nut", "qty": 4, "line": 1.0})]

def test_unexplained_skipped_row_holds_the_document():
    html = TABLE.replace("<td></td><td></td><td>Sum</td><td>$4.00</td>", "<td></td><td>9</td><td>$1.00</td><td>$9.00</td>")
    with pytest.raises(Quarantine, match="row 3 skipped but not blank"):
        extract_rows({"id": "d1", "html": html, "text": ""}, ROWS, "p.rows")

def test_css_repeated_blocks_and_lines_select():
    html = '<div class="it"><b>Lamp</b><i>x2</i></div><div class="it"><b>Desk</b><i>x1</i></div>'
    rows = extract_rows({"html": html, "text": ""}, {"select": {"css": "div.it"},
                        "fields": {"name": {"css": "b"}, "n": {"css": "i", "regex": r"x(\d+)", "cast": "int"}}}, "w")
    assert [r.fields for r in rows] == [{"name": "Lamp", "n": 2}, {"name": "Desk", "n": 1}]
    text = "Items\nLamp  2\nDesk  1\nThanks"
    rows = extract_rows({"text": text}, {"select": {"lines": {"after": "^Items$", "before": "^Thanks$",
                        "match": r"^(?P<name>.+?)\s{2,}(?P<n>\d+)$"}}, "fields": {"name": {"regex": "(.+)"}, "n": {"regex": r"(\d+)", "cast": "int"}}}, "w")
    assert [r.fields["n"] for r in rows] == [2, 1]

LINES = {"select": {"lines": {"after": "^Item\\s+Qty$", "before": "^Total", "match": r"^(?P<name>.+?)\s+(?P<n>\d+)$"}},
         "fields": {"name": {"regex": "(.+)"}, "n": {"regex": r"(\d+)", "cast": "int"}}}

def test_lines_repeated_heading_explained_wrapped_line_held():
    ok = "Item Qty\nLamp 2\n(continued)\nItem Qty\nDesk 1\nTotal 3"   # heading repeated after a page break
    with pytest.raises(Quarantine, match=r"line '\(continued\)' is not a row"):
        extract_rows({"text": ok}, LINES, "w")
    with_skip = {**LINES, "select": {"lines": {**LINES["select"]["lines"], "skip": r"^\(continued\)$"}}}
    assert [r.fields["name"] for r in extract_rows({"text": ok}, with_skip, "w")] == ["Lamp", "Desk"]
    with pytest.raises(Quarantine, match="not a row"):
        extract_rows({"text": "Item Qty\nLamp 2\n(brass, large)\nTotal 2"}, with_skip, "w")

@pytest.mark.parametrize("raw,spec,want", [("$1,299.00", "money", 1299.0), ("(12.50)", "money", -12.5),
    ("1.299,00 €", {"money": {"decimal": ","}}, 1299.0), ("03 Nov 2026", {"date": ["%d %b %Y"]}, "2026-11-03"),
    ("Austin, TX", {"split": {"sep": ",", "part": 1}}, "TX")])
def test_cleaners(raw, spec, want):
    assert clean_value(raw, spec, "f") == want

def test_ambiguous_date_and_money_are_held_not_guessed():
    with pytest.raises(Quarantine, match="ambiguous: 2026-03-04 or 2026-04-03"):
        clean_value("03/04/2026", {"date": ["%m/%d/%Y", "%d/%m/%Y"]}, "f")
    with pytest.raises(Quarantine, match="VALIDATION_FAILED|not a money"):
        clean_value("1.299,00", "money", "f")

def test_cross_checks():
    rows = [Row(1, "", {}, {"q": 2, "p": 1.5, "a": 3.0}), Row(2, "", {}, {"q": 4, "p": 0.25, "a": 1.0})]
    assert check_cross({"rule": "sum_equals", "rows_field": "a", "field": "s", "tolerance": 0.01}, {"s": 4.0}, rows) is None
    assert "3,205.00" in check_cross({"rule": "sum_equals", "rows_field": "a", "field": "s", "tolerance": 0.01}, {"s": 3205}, rows)
    assert check_cross({"rule": "product_equals", "fields": ["q", "p"], "field": "a", "tolerance": 0.01}, {}, rows) is None
    assert check_cross({"rule": "count_equals", "field": "n"}, {"n": 3}, rows) == "count_equals: 2 rows but n is 3"
```

- [ ] **Step 2: Run and confirm they fail.** Run `.venv/bin/python -m pytest tests/engine/test_rows.py tests/engine/test_clean.py -q`.
  Expected: FAIL (ImportError).
- [ ] **Step 3: Implement.**
  - `money`:
    - Strip currency symbols and ISO codes.
    - `(x)` and a trailing or leading `-` make the value negative.
    - With `decimal: "."` (the default), `,` is a thousands separator only in groups of 3. Anything else is
      refused.
  - `date` tries every format. Two that parse differently → ambiguous. None → `VALIDATION_FAILED`.
  - Nothing here imports outside `runner/` and the stdlib.
- [ ] **Step 4: Run the tests and confirm they pass.** Run
  `.venv/bin/python -m pytest tests/engine -q && .venv/bin/python -m pytest tests/test_no_ai.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** with message `Runner: rows engine (table, css, lines), cleaners, cross-checks`.
  - Until the teammate's `read_html` lands, the tests that need it carry a skip mark and use `record["tables"]`
    instead (`pytest.importorskip("runner.documents")`).

### Task G3: Compiler and runner: one document → many records

**Files:**
- Modify:
  - `compiler/templates/parsers.py.j2`, `compiler/templates/integration.py.j2`
  - `compiler/templates/tests/test_golden.py.j2`, `compiler/templates/tests/conftest.py.j2`
  - `compiler/compile.py` (`has_rows`, cross-check rule lines, `Example.expect.rows`)
  - `runner/pipeline.py`, `runner/run.py`, `runner/store.py` (`quarantined_raw` de-duplicates by raw `id`)
  - `docs/contracts.md` (V18 runtime)
- Test: `tests/engine/test_rows_pipeline.py`

**Interfaces:**
- Consumes: G1 and G2.
- Produces:
  - A generated package with rows sets `EMITS = "rows"` (otherwise the attribute is absent). `parse_<id>(record) ->
    dict` returns the document fields plus `"__rows__": list[runner.rows.Row]`. It uses
    `extract_rows(record, ROWS_<ID>, "<id>.rows", HTML_FIELD, TEXT_FIELD)`, where `ROWS_<ID>` is a literal dict.
  - `integ.validate_document(fields: dict, rows: list[Row]) -> None` raises `VALIDATION_FAILED` on the first failing
    cross-check rule. `integ.validate(fields, record, http)` is unchanged and runs per row on the merged fields.
  - `runner.pipeline.process_document(integ, record: dict, http, store) -> list[Outcome]`:
    1. A `flags` entry (`no_text_layer`, `encrypted` or `unreadable`) → one `PARSE_NO_MATCH` Outcome with the plain
       reason from the design.
    2. Route and parse. A failure → one Outcome with the document's id.
    3. `validate_document`. A failure → one Outcome with the document's id.
    4. Per row, with `rec_row = {**record, "row_no": n, "row_count": len(rows)}` and the merged fields: validate,
       then `dedupe_key`.
    5. When `hold == "document"`, every row's body is built and validated (`build_body`) **before** the first send.
       Any failure → one Outcome with the document's id, and nothing is sent.
    6. Then each row is sent and verified, giving an Outcome `"<id>#<n>"`.
  - `process_record` is unchanged. `runner.run.run` calls `process_document` when `getattr(integ, "EMITS", None) ==
    "rows"` and stores every Outcome with `raw = record` (the document).
  - Golden examples may carry `expect: {rows: [<body>, ...]}`. The golden template asserts
    `[o.body for o in outs] == rows` and `http.sink_writes == rows`.

- [ ] **Step 1: Write the failing tests.** Compile `tests/engine/data/rows_spec.yaml`, which is G2's table rows plus
  `{rule: sum_equals, rows_field: line, field: total}` with `total` read by the regex `Sum\s+\$?(\S+)` and
  `clean: money`, into a SQLite sink. Then:

```python
def test_one_document_many_outcomes(rows_pkg, fake_http):
    outs = process_document(rows_pkg, doc(TABLE), fake_http, MemoryStore())
    assert [o.record_id for o in outs] == ["doc-000001#1", "doc-000001#2"] and {o.status for o in outs} == {"success"}
    assert [b["Line"] for b in fake_http.sink_writes] == [3.0, 1.0]

def test_failed_cross_check_holds_document_writes_nothing(rows_pkg, fake_http):
    outs = process_document(rows_pkg, doc(TABLE.replace("$4.00", "$5.00")), fake_http, MemoryStore())
    assert [(o.record_id, o.reason) for o in outs] == [("doc-000001", "VALIDATION_FAILED")]
    assert "sum_equals" in outs[0].detail and "5.00" in outs[0].detail and fake_http.sink_writes == []

def test_hold_document_validates_every_row_before_any_write(rows_pkg_qty_positive, fake_http):
    outs = process_document(rows_pkg_qty_positive, doc(TABLE.replace("<td>4</td>", "<td>-4</td>")), fake_http, MemoryStore())
    assert len(outs) == 1 and outs[0].reason == "VALIDATION_FAILED" and fake_http.sink_writes == []

def test_replay_after_partial_success_writes_no_duplicates(rows_pkg, fake_http):
    store = MemoryStore()
    process_document(rows_pkg, doc(TABLE), fake_http, store)
    again = process_document(rows_pkg, doc(TABLE), fake_http, store)
    assert {o.status for o in again} == {"duplicate"} and len(fake_http.sink_writes) == 2

def test_flagged_document_is_held_with_plain_reason(rows_pkg, fake_http):
    out, = process_document(rows_pkg, {**doc(""), "flags": ["no_text_layer"]}, fake_http, MemoryStore())
    assert out.reason == "PARSE_NO_MATCH" and "no text layer" in out.detail

def test_v1_and_jobs_packages_have_no_rows_code(tmp_path):
    for p in ("fixtures/spec.yaml", "fixtures/jobs/spec.yaml"):
        res = compile_fixture(p, tmp_path)
        assert "extract_rows" not in (res.out_dir / "parsers.py").read_text() and "EMITS" not in (res.out_dir / "integration.py").read_text()
```

- [ ] **Step 2: Run and confirm they fail.** Run `.venv/bin/python -m pytest tests/engine/test_rows_pipeline.py -q`.
  Expected: FAIL.
- [ ] **Step 3: Implement.** Template branches go only under `{% if has_rows %}`, so the existing specs compile to
  byte-identical code. The `NEW_SOURCE_FORMAT` threshold counts document-level `PARSE_NO_MATCH` Outcomes, as it does
  today.
- [ ] **Step 4: Run the tests and confirm they pass.** Run `.venv/bin/python -m pytest -q`. Expected: the whole suite
  PASSES.
- [ ] **Step 5: Commit** with message `Runner: one document becomes many records; cross-checks before any write`.

### Task G4: The document inbox, the adapter, and upload (V17)

**Files:**
- Create: `control/documents.py`, `control/api/documents.py`, `control/inboxes.py`
- Modify:
  - `control/api/server.py`: include the router (shared with plan A Tasks 2–3; append only).
  - `control/autorun.py`: `is_document_spec`, and `is_page_spec` excludes documents.
  - `control/page_hooks.py`: the inbox comes from `control.inboxes.inbox_for`, and flagged documents are never
    learned.
  - `agents/deep_tools.py`: `ToolContext.get_page_record` dispatches by id prefix (shared with plan A Task 5).
  - `docs/contracts.md`: V17.
- Test: `tests/control/test_documents.py`

**Interfaces:**
- Consumes (teammate, in progress): `runner.documents.read_any(data, name, content_type) -> Document`, `Document`,
  `Table`. Until they land, tests build `Document` objects directly
  (`pytest.importorskip("runner.documents")`).
- Produces:
  - `control.documents.to_records(doc: Document, *, integration: str | None, received_at: str, sha256: str) ->
    list[dict]`. The parent comes first, then its children. Each is a V17 record without `id`/`seq`:
    - `kind`, `name`, `text`, `html` (`""` for None) and `meta` are copied unchanged.
    - `tables` = `[{"header", "rows", "where"}]`.
    - `site` = the domain of the sender address in an email's meta, inherited by its children; `"upload"`
      otherwise.
    - Children inherit the parent's `meta` keys that they don't have.
    - `flags` = `["no_text_layer"]` when `meta.get("no_text_layer") == "true"`.
    - `is_leaf`: a child of kind pdf, sheet, html or text, or the parent when it has no such child.
  - `control.documents.DocumentInbox(path=None)`, with the same method names as `PageInbox`:
    - `add_bytes(data: bytes, name: str, content_type: str, integration: str | None, received_at: str | None =
      None, site: str | None = None, meta: dict | None = None) -> dict` calls `read_any`. `site` and `meta` override or
      extend the adapter's values (table connectors, G13). If `read_any` raises, the result is one record with `flags=["unreadable"]` and
      `meta={"problem": <error>}`. The original is stored, and the parent record + `leaves: [ids]` is returned.
    - `list(since=0, limit=100, integration=None, leaves_only=True) -> list[dict]`
    - `get(doc_id) -> dict | None`, `summaries(...)`, `clear(integration=None)`
    - `add_learn(...)`, `learns(integration=None)`
    - Ids are `doc-%06d`. The store is `$PATCHWORK_WORKSPACE/documents.db`, and originals go in
      `$PATCHWORK_WORKSPACE/documents/`.
    - The same bytes (`sha256`) for one integration return the existing record with `"duplicate": true`.
  - `control.inboxes.inbox_for(record_id: str) -> PageInbox | DocumentInbox` (`pg-` / `doc-`).
  - API (same auth as `POST /api/pages`):
    - `POST /api/documents` (multipart `file`, `integration`) → 201. An integration that is not a document
      integration or a draft → 422.
    - `GET /api/documents?since=&integration=&limit=` → leaves (the runner's source).
    - `GET /api/documents/{id}`.
    - SSE `document` events.
    - A new leaf starts a run of its integration (`AutoRunner.request`), as `POST /api/pages` does.
  - `control.autorun.is_document_spec(spec) -> bool`: the source is `patchwork://control` with path
    `/api/documents`. `is_page_spec` additionally requires the path `/api/pages`.

- [ ] **Step 1: Write the failing tests**

```python
def test_adapter_marks_leaves_site_and_flags():
    pdf = Document(kind="pdf", name="a.pdf", text="", html=None, tables=[], meta={"no_text_layer": "true"}, children=[])
    mail = Document(kind="email", name="m", text="see attached", html=None, tables=[],
                    meta={SENDER_KEY: "Billing <b@north.example>", "subject": "S"}, children=[pdf])
    parent, child = to_records(mail, integration="x", received_at="2026-11-14T00:00:00+00:00", sha256="h")
    assert (parent["is_leaf"], child["is_leaf"]) == (False, True)
    assert child["site"] == "north.example" and child["flags"] == ["no_text_layer"] and child["meta"]["subject"] == "S"

def test_csv_upload_lists_as_runner_source_and_starts_a_run(client, doc_integration, auto_spy):
    r = client.post("/api/documents", files={"file": ("p.csv", b"Name\nAda\n", "text/csv")}, data={"integration": doc_integration}, headers=TOKEN)
    assert r.status_code == 201
    listed = client.get(f"/api/documents?since=0&integration={doc_integration}").json()
    assert [d["id"] for d in listed] == r.json()["leaves"] and listed[0]["tables"][0]["header"] == ["Name"]
    assert auto_spy.requests == [doc_integration]

def test_same_file_twice_is_one_document(client, doc_integration):
    a = client.post("/api/documents", files={"file": ("p.csv", b"Name\nAda\n")}, data={"integration": doc_integration}, headers=TOKEN).json()
    b = client.post("/api/documents", files={"file": ("p.csv", b"Name\nAda\n")}, data={"integration": doc_integration}, headers=TOKEN).json()
    assert a["id"] == b["id"] and b["duplicate"] is True

def test_unreadable_upload_is_kept_and_flagged(client, doc_integration):
    d = client.post("/api/documents", files={"file": ("x.pdf", b"not a pdf")}, data={"integration": doc_integration}, headers=TOKEN).json()
    assert d["flags"] == ["unreadable"] and d["meta"]["problem"]

def test_page_and_document_specs_are_told_apart():
    assert is_page_spec(JOBS_SPEC) and not is_document_spec(JOBS_SPEC)
    assert is_document_spec(DOC_SPEC) and not is_page_spec(DOC_SPEC)

def test_flagged_document_never_learned(hooks, flagged_doc_id, learn_spy):
    assert hooks.maybe_learn("doc_int", [flagged_doc_id]) == [] and learn_spy.calls == []
```

  `SENDER_KEY` is the meta key `read_eml` uses for the From address. Confirm it from the teammate's code.

- [ ] **Step 2: Run and confirm they fail.** Run `.venv/bin/python -m pytest tests/control/test_documents.py -q`.
  Expected: FAIL.
- [ ] **Step 3: Implement.** `DocumentInbox` copies `PageInbox`'s connection and migration pattern. Columns: `seq`,
  `parent_seq`, `kind`, `integration`, `site`, `name`, `text`, `html`, `tables` (JSON), `meta` (JSON), `flags`
  (JSON), `sha256`, `received_at`, `received_date`, `is_leaf`. HTML and text are never logged.
- [ ] **Step 4: Run the tests and confirm they pass.** Run `.venv/bin/python -m pytest tests/control tests/engine -q`.
  Expected: PASS, including `test_api_pages.py`, `test_pages_routing.py` and `test_page_hooks_platform.py`.
- [ ] **Step 5: Commit** with message `Documents: inbox and adapter over runner.documents, upload API (contracts V17)`.

### Task G5: LEARN checks and tools for documents and rows

**Files:**
- Modify:
  - `agents/learn.py`: `document_outline`, `rows_checks`, document evidence, `d2_decision` documents.
  - `agents/deep_tools.py`: `get_page`, `test_page_parser`, `save_parser`. Extract the shared check helper
    `check_candidate` here; plan A Task 5's `propose_mapping`/`add_site_parser` call it too.
  - `agents/interface.py`: the `evidence` event gains an optional `rows`.
- Test: `tests/agents/test_learn_rows.py`

**Interfaces:**
- Consumes: `runner.rows.extract_rows`, `explain_skip`, `check_cross`; `ToolContext.get_page_record` (G4).
- Produces:
  - `agents.learn.document_outline(record: dict, max_chars: int = 4000) -> str`:
    - the meta lines (sender, subject, filename);
    - the text lines numbered `L1..` (capped);
    - per table: `T<n> columns: [...] rows: N`, with its first 3 rows;
    - the HTML outline (via `page_outline`) when there is HTML.
  - `agents.learn.run_document_parser(parser: dict, record: dict, html_field="html", text_field="text") -> dict`:
    `{"fields": {...}, "rows": [Row...]}` or `{"error": ...}`.
  - `agents.learn.rows_checks(record: dict, parser: dict, rows: list[Row], validate: list[dict]) -> list[dict]`, one
    item `{name, ok, detail}` per check:
    - `values_in_own_row`
    - `required_present` (≥ 95% of rows per required field)
    - `no_duplicate_rows`
    - `skips_explained`
    - `row_count`: the selected region's data rows minus explained skips equal `len(rows)`
    - one item per declared cross-check rule
  - `agents.learn.evidence` accepts document records. A value may also occur in a table cell or a `meta` string;
    `where` gains `"table"` and `"meta"`.
  - `d2_decision(..., is_document: bool = False, cross_checks_passed: int = 0)`: for documents, auto requires
    `cross_checks_passed >= 1` in place of `all_corroborated`.
  - `check_candidate(parser: dict, rec: dict, spec: dict) -> dict` (in `deep_tools`) returns `{fields, rows,
    evidence, rows_checks, ambiguous, evidence_ok, rows_ok, all_corroborated, cross_checks_passed}`.
    - A document-level regex that matches twice with different values counts as ambiguous.
  - The tools:
    - `test_page_parser` reports the rows checks and emits `evidence` with `rows: {count, checks}`.
    - `save_parser` refuses when `rows_ok` is false (`REFUSED: rows check <name> failed: <detail>`). Its golden
      example is `expect: {rows: [...]}`.

- [ ] **Step 1: Write the failing tests** on a CSV document and an HTML-body document, using G2's `TABLE` and a spec
  with `sum_equals`:

```python
def test_good_rows_parser_passes_every_check(learn_ctx_doc):
    out = learn_ctx_doc.call("test_page_parser", parser=GOOD)
    assert out.startswith("OK") and '"sum_equals": true' in out.replace(" ", "")

def test_parser_including_footer_row_refused_by_cross_check(learn_ctx_doc):
    out = learn_ctx_doc.call("save_parser", parser=with_require([], GOOD))  # keeps the "Sum" row
    assert out.startswith("REFUSED") and "sum_equals" in out

def test_value_from_another_row_refused(learn_ctx_doc):
    out = learn_ctx_doc.call("save_parser", parser=reading_qty_from_first_row_always(GOOD))
    assert out.startswith("REFUSED") and "values_in_own_row" in out

def test_wrong_column_mapping_refused_by_required_present(learn_ctx_doc):
    assert "required_present" in learn_ctx_doc.call("save_parser", parser=mapping_item_to_empty_column(GOOD))

def test_auto_deploy_needs_a_passing_cross_check(learn_ctx_doc, learn_ctx_doc_no_rules):
    learn_ctx_doc.call("save_parser", parser=GOOD)
    assert learn_ctx_doc.learn_result["approval"] == "auto"
    learn_ctx_doc_no_rules.call("save_parser", parser=GOOD)
    assert learn_ctx_doc_no_rules.learn_result["approval"] in ("pending", "human")

def test_page_learn_unchanged(learn_ctx_page):  # V10 behaviour on a jobs page
    assert learn_ctx_page.call("test_page_parser", parser=JOBS_PARSER).startswith("OK")
```

- [ ] **Step 2: Run and confirm they fail.** Run `.venv/bin/python -m pytest tests/agents/test_learn_rows.py -q`.
  Expected: FAIL.
- [ ] **Step 3: Implement.** Factor `save_parser`'s checks into `check_candidate` without changing a page result
  (the existing learn tests are the guard).
- [ ] **Step 4: Run the tests and confirm they pass.** Run `.venv/bin/python -m pytest tests/agents -q`. Expected:
  PASS.
- [ ] **Step 5: Commit** with message `LEARN: document outline, rows checks and declared cross-checks in code`.

### Task G6: The deep agent and the scripted agent on documents; the no-domain-words test

**Files:**
- Modify:
  - `agents/prompts.py`: `document_learner_prompt(spec)` (shared with plan A Task 6).
  - `agents/deep.py`: `_run_learn` picks it when the page id starts with `doc-`.
  - `agents/scripted_v2.py`: `propose_rows_parser` (shared with plan A Task 7).
  - `agents/deep_tools.py`: `TOOL_DOCS` (create it if plan A Task 6 hasn't).
- Test:
  - `tests/agents/test_deep_learn_documents.py`: an offline `ScriptedChatModel`.
  - `tests/agents/test_scripted_learn_documents.py`
  - `tests/test_no_domain_words.py`

**Interfaces:**
- Produces:
  - `agents.prompts.document_learner_prompt(spec: dict) -> str`. It names only the spec's intent, fields (document
    and row) and rules.
  - `ScriptedV2Mixin.propose_rows_parser(rec: dict, spec: dict) -> dict | None`:
    - It picks the table whose header best matches the row fields: humanized names first, then the column names the
      existing parsers use.
    - `clean: money` is set when at least 80% of a column's cells look like money.
    - `cast: int` is set for all-integer columns.
    - **No table (a PDF's text):** it builds a `lines` selector.
      - The rows are the longest run of consecutive lines that share one token shape: the same count of number-like
        tokens at the end.
      - `after` and `before` are anchored, escaped first words of the lines around that run (`^Item\b`, `^Sum\b`),
        never their values.
      - `match` gets one named group per trailing number, plus a leading text group.
      - The groups are named after the heading line's words (`item`, `qty`, `each`, `line`), or else after an
        existing parser's `lines.match` groups. Row fields read them with `column`.
    - Document fields come from `<Label>\s*[:#]?\s*(\S+)` lines, with labels taken from the field's humanized name
      or the existing parsers' regex labels.
    - It returns `None` when the checks fail, and the document stays held.
  - `propose_parser` calls it when the spec has rows or the record is a document.
  - `DOMAIN_WORDS = r"\b(invoices?|vendors?|suppliers?|purchase|shipping|shipments?|carriers?|tickets?|attendees?|registrations?|customers?|subtotal|tax|prices?)\b"`
    (case-insensitive) in `tests/test_no_domain_words.py`.

- [ ] **Step 1: Write the failing tests**

```python
def test_deep_learn_on_document_auto_deploys(fake_model_service, doc_learn_case):
    script = [turn(call("task", subagent_type="learner", description="learn")), turn(call("get_page")),
              turn(call("test_page_parser", parser=GOOD)), turn(call("save_parser", parser=GOOD, reason="new layout")),
              turn(text="saved"), turn(text="done")]
    ev = run_learn(fake_model_service(script), doc_learn_case)
    ev_rows = next(e for e in ev if e["type"] == "evidence")["rows"]
    assert ev_rows["count"] == 2 and all(c["ok"] for c in ev_rows["checks"])
    assert next(e for e in ev if e["type"] == "learned")["approval"] == "auto"
    assert sum(e["type"] == "model_call" for e in ev) <= 6

def test_scripted_learn_emits_same_event_types(scripted_service, doc_learn_case, fake_model_events):
    ev = run_learn(scripted_service, doc_learn_case)
    assert [e["type"] for e in ev if e["type"] in ("evidence", "learned", "deployed")] == \
           [e["type"] for e in fake_model_events if e["type"] in ("evidence", "learned", "deployed")]

def test_scripted_proposes_lines_for_text_without_tables(scripted_service, rows_spec):
    rec = {"id": "doc-000009", "kind": "pdf", "site": "b.example", "tables": [], "html": "",
           "text": "Ref R-9\nItem Qty Each Line\nBolt 2 1.50 3.00\nNut 4 0.25 1.00\nSum 4.00"}
    p = scripted_service.propose_rows_parser(rec, rows_spec)
    assert "lines" in p["rows"]["select"]
    assert [r.fields["line"] for r in extract_rows(rec, p["rows"], "w")] == [3.0, 1.0]

INPUT_LAYER = ["runner/documents.py", "runner/imap.py", "control/connectors", "control/documents.py", "control/inboxes.py",
               "runner/rows.py", "runner/clean.py"]
def test_input_layer_has_no_domain_words():
    for f in python_files(INPUT_LAYER):
        assert not DOMAIN_WORDS.search(f.read_text()), f
    from agents import prompts, deep_tools
    assert not DOMAIN_WORDS.search(prompts.document_learner_prompt({"intent": "", "extract": {"parsers": []}}))
    for t in ("get_page", "test_page_parser", "save_parser"):
        assert not DOMAIN_WORDS.search(deep_tools.TOOL_DOCS[t]), t
```

- [ ] **Step 2: Run and confirm they fail.** Expected: FAIL.
- [ ] **Step 3: Implement.** `test_no_domain_words.py` skips the paths that don't exist yet (phases 2–4 add them), so
  it guards each one as it lands.
- [ ] **Step 4: Run the tests and confirm they pass.** Run `.venv/bin/python -m pytest tests/agents tests/test_no_domain_words.py -q`.
  Expected: PASS.
- [ ] **Step 5: Commit** with message `Agents: LEARN on documents (deep + scripted, same events); no domain words in the input layer`.

### Task G7: The phase 1 proof: tables end to end

**Files:**
- Create:
  - `fixtures/tables/`: `orders-a.html` (`<table>` with a footer row), `orders-b.html` (repeated `div` blocks),
    `people.csv`, `people.xlsx`, `spec_html.yaml`, `spec_csv.yaml`.
  - `fixtures/tables/make.py`, which regenerates `people.xlsx` with openpyxl (the teammate's dependency).
- Test: `tests/control/test_rows_e2e.py`, in-process control plane with the scripted agent.

**Interfaces:**
- Consumes: G3, G4, G5, G6; `runner.documents.read_sheet`/`read_html` (teammate); `demo_apps/fake_airtable`;
  `demo_apps/sqlite_tracker`.

- [ ] **Step 1: Write the failing tests**
  - **HTML pages:**
    1. A deployed `spec_html` reads site A (a page through `POST /api/pages`). The page's rows are written to fake
       Airtable.
    2. A page from site B is held as `PARSE_NO_MATCH`. A scripted LEARN learns the `css` rows.
    3. After the replay, B's rows are written, and Airtable has exactly A's rows plus B's.
  - **CSV:** uploading `people.csv` and `people.xlsx` for a `spec_csv` → SQLite integration writes one row per data
    row. Uploading `people.csv` again writes nothing new.
- [ ] **Step 2: Run and confirm they fail.** Run `.venv/bin/python -m pytest tests/control/test_rows_e2e.py -q`.
  Expected: FAIL.
- [ ] **Step 3: Implement** whatever wiring is missing. Expected gaps are the replay de-duplication by raw `id`
  (G3) and autorun on document arrival (G4).
- [ ] **Step 4: Run the full suite.** Run `.venv/bin/python -m pytest -q && .venv/bin/python scripts/smoke_v2.py --in-process && .venv/bin/python scripts/smoke_v2.py --sink airtable --in-process`.
  Expected: all PASS. **Phase 1 is demoable here.**
- [ ] **Step 5: Commit** with message `Phase 1: tables end to end (HTML tables and blocks, CSV, XLSX), new layout learned`.

## Teammate, in progress (consumed by G4, G8–G12; no task here edits these)

- **`runner/documents.py`:** `Table`, `Document`, `read_eml`, `read_pdf` (pypdf text layer;
  `meta["no_text_layer"]="true"` when empty), `read_sheet` (.csv/.xlsx via openpyxl), `read_html`, `read_any`. PDF
  table extraction is out of scope: line items are learned from text.
- **`runner/imap.py`** (optional): `fetch_new(host, user, credential, folder="INBOX", since_uid=0) -> list[tuple[int,
  bytes]]`.
- **`fixtures/emails/`**, from `scripts/make_email_fixtures.py`, each email with its `<name>.expected.json`:
  - invoices (a body table, and a PDF attachment)
  - order and shipping confirmations with HTML item tables
  - event registrations
  - support tickets

  Each kind comes from 2 senders. There is also one `.xlsx` attachment and one image-only PDF.
- **Dependencies:** `pypdf`, `openpyxl` (runtime), `fpdf2` (dev).

When a task below needs one of these before it lands, its tests use `pytest.importorskip("runner.documents")` or
skip when `fixtures/emails/` is missing. They never re-implement it.

## Phase 2: PDF

### Task G8: PDF line items from text through the pipeline

**Files:**
- Create: `tests/engine/data/pdf_lines_spec.yaml`
- Test: `tests/engine/test_pdf_documents.py`

**Interfaces:**
- Consumes: `runner.documents.read_pdf` (teammate), G2's `lines` selector, G3's `process_document`, G4's
  `to_records`; `fpdf2` (dev) for in-test PDFs.

- [ ] **Step 1: Write the failing tests.** The PDFs are made in the test with fpdf2, then put through `read_pdf` →
  `to_records` → `process_document` (FakeHttp, SQLite sink).

```python
def test_text_pdf_line_items_become_rows(pdf_pkg, fake_http):
    rec = leaf_record(read_pdf(make_pdf(HEAD + ITEMS_3 + TOTALS), "a.pdf"))
    outs = process_document(pdf_pkg, rec, fake_http, MemoryStore())
    assert [o.status for o in outs] == ["success"] * 3 and [b["Amount"] for b in fake_http.sink_writes] == [90.0, 125.0, 3000.0]

def test_heading_repeated_on_page_two_is_explained(pdf_pkg, fake_http):
    rec = leaf_record(read_pdf(make_pdf(HEAD + ITEMS_3[:2], page_break=True, then=HEAD_LINE + ITEMS_3[2:] + TOTALS), "b.pdf"))
    assert len(process_document(pdf_pkg, rec, fake_http, MemoryStore())) == 3

def test_wrapped_description_holds_the_document(pdf_pkg, fake_http):
    rec = leaf_record(read_pdf(make_pdf(HEAD + WRAPPED_ITEM + TOTALS), "c.pdf"))
    (out,) = process_document(pdf_pkg, rec, fake_http, MemoryStore())
    assert out.reason == "PARSE_NO_MATCH" and "not a row" in out.detail and fake_http.sink_writes == []

def test_image_only_pdf_held_with_plain_reason(pdf_pkg, fake_http):
    (out,) = process_document(pdf_pkg, leaf_record(read_pdf(make_image_only_pdf(), "scan.pdf")), fake_http, MemoryStore())
    assert out.reason == "PARSE_NO_MATCH" and "no text layer" in out.detail
```

- [ ] **Step 2: Run and confirm they fail.** Run `.venv/bin/python -m pytest tests/engine/test_pdf_documents.py -q`.
  Expected: FAIL. Skipped instead while `runner.documents` hasn't landed.
- [ ] **Step 3: Implement** only what is missing in G2, G3 and G4. Report any pypdf text shape the `lines` selector
  can't express (e.g. the columns of one row split over two text lines) to the lead and the teammate. Don't work
  around it in the reader.
- [ ] **Step 4: Run the tests and confirm they pass.** Run `.venv/bin/python -m pytest tests/engine -q`. Expected:
  PASS.
- [ ] **Step 5: Commit** with message `PDF documents: line items from text, page breaks explained, image-only held`.

## Phase 3: email

### Task G9: Email leaves end to end (read_eml → records → rows)

**Files:**
- Test: `tests/control/test_email_documents.py`

**Interfaces:**
- Consumes: `runner.documents.read_eml` (teammate), the files in `fixtures/emails/`, G4's `to_records` and
  `DocumentInbox`.

- [ ] **Step 1: Write the failing tests** over every fixture email:

```python
@pytest.mark.parametrize("eml", sorted(Path("fixtures/emails").glob("*.eml")))
def test_every_fixture_email_has_leaves_with_a_site(eml, inbox):
    rec = inbox.add_bytes(eml.read_bytes(), eml.name, "message/rfc822", integration="x")
    leaves = [inbox.get(i) for i in rec["leaves"]]
    assert leaves and all(l["site"] and l["site"] != "upload" for l in leaves)

def test_pdf_attachment_is_the_leaf_and_inherits_subject(inbox):
    rec = inbox.add_bytes(fixture_email(kind="invoice", attachment="pdf"), "i.eml", "message/rfc822", integration="x")
    (leaf,) = [inbox.get(i) for i in rec["leaves"]]
    assert leaf["kind"] == "pdf" and leaf["meta"].get("subject")

def test_image_only_pdf_email_leaf_is_flagged(inbox):
    rec = inbox.add_bytes(fixture_email(image_only_pdf=True), "s.eml", "message/rfc822", integration="x")
    assert [inbox.get(i)["flags"] for i in rec["leaves"]] == [["no_text_layer"]]
```

  `fixture_email(...)` picks a file in `fixtures/emails/` by its `.expected.json` (kind, attachment). It is a helper
  in `tests/control/email_fixtures.py`.
- [ ] **Step 2: Run and confirm they fail.** Expected: FAIL. Skipped instead until `fixtures/emails/` exists.
- [ ] **Step 3: Implement** what is missing in `to_records`, typically the sender meta key and the leaf rule.
- [ ] **Step 4: Run the tests and confirm they pass.** Run `.venv/bin/python -m pytest tests/control/test_email_documents.py tests/control/test_documents.py -q`.
  Expected: PASS.
- [ ] **Step 5: Commit** with message `Email documents: leaves, sender site, flags over the teammate's reader`.

### Task G10: Connectors: a folder of `.eml` files and IMAP (V19)

**Files:**
- Create:
  - `control/connectors/__init__.py`, `control/connectors/base.py`
  - `control/connectors/eml_dir.py`, `control/connectors/imap.py`
  - `control/api/connectors.py`
- Modify: `control/api/server.py` (router; poller started in lifespan; append only), `docs/contracts.md` (V19)
- Test: `tests/control/test_connectors.py`

**Interfaces:**
- Consumes: `runner.imap.fetch_new` (teammate, optional); G4's `DocumentInbox.add_bytes`.
- Produces:
  - `control.connectors.base.Connector` (Protocol): `poll() -> list[tuple[str, bytes, str]]` returns `(name, data,
    content_type)` for new items only. `state() -> dict` is persisted.
  - `control.connectors.ConnectorConfig` (pydantic):
    - `integration: str`, `kind: Literal["eml_dir","imap","csv_url","airtable","sqlite"]`
    - `path?`, `host?`, `user?`, `credential?` (V13 name), `folder: str = "INBOX"`
    - `from_domains: list[str] = []`, `subject_regex: str | None`
    - `every_s: float = 2.0`
  - `control.connectors.poll_once(cfg: ConnectorConfig, inbox: DocumentInbox, *, fetch=None) -> dict`: `{"added":
    [ids], "skipped": n, "error": str | None}`.
    - Filters apply to the leaf records' `site` and meta subject.
    - A filtered-out document is removed again, and it is never run.
    - The error names a credential, never its value (`runner.credentials.redact`).
  - `EmlDirConnector(path)`:
    - It reads `*.eml` files whose mtime is at least 0.5 s old.
    - Each file sha256 is ingested once (the inbox's duplicate rule).
    - The path must be inside the workspace or a directory the config names.
  - `ImapConnector(host, user, credential, folder, fetch=runner.imap.fetch_new)`: it keeps `since_uid` in its
    state.
  - `control.connectors.Poller(configs_dir, inbox, on_new: Callable[[str], None])` runs a daemon thread. New leaves
    call `on_new(integration)`.
  - API:
    - `PUT /api/integrations/{name}/connector {ConnectorConfig}` → `{ok, error?}`, after one `poll_once`.
    - `GET /api/integrations/{name}/connector`.
    - Configs are stored as `$PATCHWORK_WORKSPACE/connectors/<name>.json`.

- [ ] **Step 1: Write the failing tests**

```python
def test_eml_dir_ingests_each_message_once(tmp_path, inbox):
    cfg = ConnectorConfig(integration="x", kind="eml_dir", path=str(tmp_path))
    src = first_fixture_email()
    write_old(tmp_path / "a.eml", src.read_bytes())
    assert len(poll_once(cfg, inbox)["added"]) >= 1
    write_old(tmp_path / "a-again.eml", src.read_bytes())  # delivered twice
    assert poll_once(cfg, inbox)["added"] == []

def test_partial_file_waits(tmp_path, inbox):
    (tmp_path / "b.eml").write_bytes(first_fixture_email().read_bytes()[:40])  # mtime: now
    assert poll_once(ConnectorConfig(integration="x", kind="eml_dir", path=str(tmp_path)), inbox)["added"] == []

def test_imap_tracks_uids(inbox):
    calls = []
    def fetch(host, user, credential, folder="INBOX", since_uid=0):
        calls.append(since_uid); return [(7, first_fixture_email().read_bytes())] if since_uid < 7 else []
    cfg = ConnectorConfig(integration="x", kind="imap", host="imap.example", user="me@example.com", credential="MAIL_APP_PASSWORD")
    assert poll_once(cfg, inbox, fetch=fetch)["added"] and poll_once(cfg, inbox, fetch=fetch)["added"] == [] and calls == [0, 7]

def test_imap_error_names_credential_not_value(inbox, monkeypatch):
    monkeypatch.setenv("MAIL_APP_PASSWORD", "sentinel-mail-123456")
    def fetch(*a, **k): raise RuntimeError("LOGIN failed for sentinel-mail-123456")
    err = poll_once(IMAP_CFG, inbox, fetch=fetch)["error"]
    assert "MAIL_APP_PASSWORD" in err and "sentinel-mail-123456" not in err

def test_filters_apply_before_any_run(tmp_path, inbox, auto_spy):
    cfg = ConnectorConfig(integration="x", kind="eml_dir", path=str(tmp_path), from_domains=["nobody.example"])
    write_old(tmp_path / "n.eml", first_fixture_email().read_bytes())
    assert poll_once(cfg, inbox)["added"] == [] and auto_spy.requests == []
```

- [ ] **Step 2: Run and confirm they fail.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests and confirm they pass.** Run `.venv/bin/python -m pytest tests/control tests/test_no_domain_words.py -q`.
  Expected: PASS.
- [ ] **Step 5: Commit** with message `Connectors: .eml folder and IMAP (via runner.imap), filters, poller (contracts V19)`.

### Task G11: Four email kinds, two senders each, end to end

**Files:**
- Create:
  - `fixtures/email_specs/`: `invoice_lines.yaml` (fake Airtable `Invoice Lines`), `shipment_items.yaml` (SQLite),
    `registrations.yaml` (SQLite), `tickets.yaml` (SQLite, no rows). Each has **the first sender's parser only**,
    written by hand against the teammate's fixtures.
  - `tests/control/email_stack.py`: an in-process control plane, an `.eml` folder connector, the scripted agent,
    fake Airtable, and a temporary SQLite file.
- Modify:
  - `demo_apps/fake_airtable/app.py`: `create_app(tables=...)` accepts extra tables (`Invoice Lines`).
  - `demo_apps/sqlite_tracker/__main__.py`: `--init-table <name> --columns <c:type,...>`.
- Test: `tests/control/test_email_kinds_e2e.py`

**Interfaces:**
- Consumes: G3–G10; `fixtures/emails/*.eml` and `*.expected.json` (teammate). The expected JSON is the oracle.
  Mapping its keys to sink columns is the only fixture-specific code, and it lives in the test.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("kind", ["invoice_lines", "shipment_items", "registrations", "tickets"])
def test_second_sender_learned_once_then_plain_code(kind, stack):
    first, second = senders(kind)                       # from fixtures/emails/*.expected.json
    stack.deploy(f"fixtures/email_specs/{kind}.yaml")
    stack.deliver(first); stack.wait_idle(kind)
    stack.deliver(second); stack.wait_idle(kind)        # held -> scripted LEARN -> replay
    assert stack.rows(kind) == expected_rows(kind, [first, second])
    assert stack.learns(kind) == [site_of(second)] and stack.run_summaries(kind)[-1]["llm_calls"] == 0

def test_bad_sum_variant_held_nothing_written(stack):
    body_sender = invoice_with_body_table()
    stack.deploy("fixtures/email_specs/invoice_lines.yaml"); stack.deliver(body_sender); stack.wait_idle("invoice_lines")
    n = len(stack.rows("invoice_lines"))
    stack.deliver_bytes(change_one_line_amount(body_sender.read_bytes())); stack.wait_idle("invoice_lines")
    held = stack.held("invoice_lines")
    assert held[-1]["reason"] == "VALIDATION_FAILED" and "sum_equals" in held[-1]["detail"] and len(stack.rows("invoice_lines")) == n

def test_image_only_pdf_held_never_learned(stack):
    stack.deploy("fixtures/email_specs/invoice_lines.yaml"); stack.deliver(image_only_email()); stack.wait_idle("invoice_lines")
    (h,) = stack.held("invoice_lines")
    assert "no text layer" in h["detail"] and stack.learns("invoice_lines") == []

def test_extra_pdf_next_to_invoice_held_invoice_written(stack):
    pdf_sender = invoice_with_pdf()
    stack.deploy_with_learned("invoice_lines", pdf_sender)
    stack.deliver_bytes(add_attachment(pdf_sender.read_bytes(), "terms.pdf", fpdf_text_pdf(["Terms and conditions"])))
    stack.wait_idle("invoice_lines")
    assert stack.rows("invoice_lines")[-len(expected_rows("invoice_lines", [pdf_sender])):] == expected_rows("invoice_lines", [pdf_sender])
    assert [h["reason"] for h in stack.held("invoice_lines")] == ["PARSE_NO_MATCH"]

def test_spreadsheet_attachment_rows_written(stack):
    stack.deploy("fixtures/email_specs/shipment_items.yaml"); stack.deliver(xlsx_email()); stack.wait_idle("shipment_items")
    assert stack.rows("shipment_items") == expected_rows("shipment_items", [xlsx_email()])
```

- [ ] **Step 2: Run and confirm they fail.** Expected: FAIL.
- [ ] **Step 3: Implement** whatever wiring is missing.
  - If the scripted agent's generic matching can't learn a second sender, improve `propose_rows_parser`'s generic
    rules (G6). **Never add subject words**; `test_no_domain_words` guards this.
  - If the `.xlsx` email belongs to a different kind than `shipment_items`, point
    `test_spreadsheet_attachment_rows_written` at that kind's spec.
- [ ] **Step 4: Run the tests and confirm they pass.** Run `.venv/bin/python -m pytest -q`. Expected: PASS.
- [ ] **Step 5: Commit** with message `Four email kinds, two senders each: learned once, then plain code; held cases`.

### Task G12: The demo: invoices → Airtable, shipping notices → SQLite

**Files:**
- Create: `scripts/airtable_tables.py` (Meta API: create a table from a field list; `--dry-run` prints the request)
- Modify:
  - `control/api/demo.py`:
    - `POST /api/demo/documents/deliver {integration, fixture, variant?: "changed_line"}` copies a file from
      `fixtures/emails/` into the integration's watched folder. With `variant`, it writes the body-table invoice
      with one line amount changed (an HTML edit) instead.
    - `POST /api/demo/documents/reset?integration=`.
  - `scripts/smoke_v2.py`: `--documents {invoice_lines,shipment_items}` (shared with plan A Task 9).
  - `docs/demo-v2.md`, `web/src/components/Presenter.tsx` (shared with plan A Task 12), `README.md`, `ROADMAP.md`.
- Test:
  - `tests/control/test_demo_documents.py`: deliver and reset, in-process.
  - `tests/test_airtable_tables_script.py`: `--dry-run` builds the exact request, with no network.

**Interfaces:**
- Produces:
  - `scripts/airtable_tables.py create --base appH3aUzXPLlI5Y2N --table "Invoice Lines" --fields "Invoice No:singleLineText,Sender:singleLineText,Invoice Date:date,Line:number,Description:singleLineText,Qty:number,Unit Price:currency,Amount:currency" [--dry-run]`.
    - It uses `POST https://api.airtable.com/v0/meta/bases/{base}/tables` with `AIRTABLE_TOKEN`, and never prints
      the token.
    - `number` and `currency` get `options: {precision: 2}`, `currency` also gets `symbol: "$"`, and `date` gets
      `{dateFormat: {name: "iso"}}`.

- [ ] **Step 1: Write the failing tests.**
  - `--dry-run` prints a JSON body whose `fields[6]` is `{"name": "Unit Price", "type": "currency", "options":
    {"precision": 2, "symbol": "$"}}`.
  - Deliver puts the PDF invoice sender's fixture email in the watched folder, and the connector ingests it within
    3 s.
  - The `changed_line` variant is held with `sum_equals` in its detail.
  - Reset clears that integration's documents and its rows in fake Airtable.
- [ ] **Step 2: Run and confirm they fail.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them offline, then live (with the owner present only).**
  - **Offline:** `.venv/bin/python -m pytest tests/control/test_demo_documents.py tests/test_airtable_tables_script.py -q`
    and `.venv/bin/python scripts/smoke_v2.py --documents invoice_lines --in-process`. Expected: PASS.
  - **Live, owner present:**
    - Create `Invoice Lines` in `appH3aUzXPLlI5Y2N`. Group the view by Invoice No (by hand).
    - On a free port (never 38091) and a separate workspace, with `PATCHWORK_MODEL=anthropic:claude-haiku-5-5`, run
      `smoke_v2.py --documents invoice_lines` against the real base.
    - Record the LEARN seconds and model calls in `bench/v2/results.json` (`learn_document_s`,
      `learn_document_calls`).
    - Target: ≤ 15 s and ≤ 6 calls. If it is slower, cap the outline at 2,500 characters and say so in
      `docs/demo-v2.md`.
- [ ] **Step 5: Update the run sheet and the presenter script.** Beat 1 is invoices → Airtable, as in the design.
  Beat 2 is `shipment_items` → SQLite, either as a beat or as the named test. The 2-minute total stays ≤ 2:00.
- [ ] **Step 6: Commit** with message `Demo: invoices by email to Airtable, shipping notices to SQLite; run sheet and timings`.

## Phase 4: table sources (optional; cut first)

### Task G13: Google Sheets CSV URL, Airtable list records, SQLite `SELECT`

**Files:**
- Create: `control/connectors/csv_url.py`, `control/connectors/airtable_list.py`, `control/connectors/sqlite_select.py`
- Test: `tests/control/test_table_connectors.py`, using an in-process CSV server on a free port, `demo_apps/fake_airtable`
  and a temporary SQLite file.

**Interfaces:**
- Produces: each connector's `poll()` yields **one item per new row**: a one-row CSV (`text/csv`), which G4 turns
  into a `sheet` document through the teammate's `read_sheet`. The item carries:
  - `meta.row_key`: the Airtable record id, the SQLite rowid, or the sha256 of the CSV row's cells.
  - `site`: the source's host, or `sqlite`.

  Seen row keys are kept in the connector state, so a re-poll adds only new rows. `SqliteSelect(path, query)`
  accepts only one `SELECT` statement, opened read-only (`mode=ro`). Airtable pages through `offset`.

- [ ] **Step 1: Write the failing tests**
  - A CSV served on a free port with 3 rows gives 3 documents. When a row is appended, the next poll gives 1 more.
  - For SQLite, `DELETE FROM t` is refused (`only one SELECT statement`).
  - Airtable with two pages (fake, `pageSize=2`) gives every record once.
- [ ] **Step 2: Run and confirm they fail.** Then implement, then run
  `.venv/bin/python -m pytest tests/control/test_table_connectors.py tests/test_no_domain_words.py -q`. Expected:
  PASS.
- [ ] **Step 3: Commit** with message `Connectors: table sources, one document per new row`.

## The wizard (after plan A Tasks 2, 4, 5, 10 and 11)

### Task G14: "Where does the data come from?" and TEACH from document examples

**Files:**
- Modify:
  - `control/drafts.py`, `control/api/drafts.py` (plan A Task 2/4 files).
  - `agents/deep_tools.py`: `propose_mapping` gets the optional `parser`; `add_site_parser` gets rows, via
    `check_candidate` (plan A Task 5).
  - `agents/scripted_v2.py`: `_build_from_examples` handles documents (plan A Task 7).
  - `web/src/pages/NewIntegration.tsx`, `web/src/api.ts`, `web/mock/v3.mjs` (plan A Task 11).
  - `web/src/components/ReviewTable.tsx`: one column per example *row* for a rows parser, plus cross-check badges.
- Test:
  - `tests/control/test_draft_sources.py`
  - `tests/agents/test_teach_document_examples.py`
  - `web/src/pages/NewIntegration.test.tsx` (extend)

**Interfaces:**
- Consumes: plan A's `Draft`, `write_teach_inputs(name, page_ids)`, `propose_mapping`, `add_site_parser`,
  `request_deploy(review)`; G4's `/api/documents`; G10's connector API.
- Produces:
  - `Draft.source: dict | None`: `{kind: "pages"} | {kind: "email", connector: ConnectorConfig} | {kind: "files"} |
    {kind: "table", connector: ConnectorConfig}`.
  - `PUT /api/integrations/{name}/source` saves it and, for email or table, runs `PUT .../connector` (G10).
  - `write_teach_inputs(name, example_ids: list[str])` accepts `doc-` ids. The candidates' `examples[]` gain `kind`
    and `record`.
  - `draft_page_spec` writes the `/api/documents` source when the examples are documents.
  - `propose_mapping(mapping, parser: dict | None = None)`:
    - With document examples, `parser` (document fields + `rows`) is checked on the first example with
      `check_candidate`, and the mapping entries may be `{"field": f}` for its fields.
    - Without `parser`, a document example → `REFUSED: document examples need a parser (fields and rows)`.
  - The review table's rows carry `checks: [{name, ok, detail}]` from `rows_checks`.

- [ ] **Step 1: Write the failing tests**
  - **Draft sources:**
    1. Create a draft and `PUT` an email source on a temporary `.eml` folder.
    2. Deliver `globex-1.eml`. It is listed under the draft as an example, and **no run starts** (plan A's draft
       rule).
    3. Teach with that `doc-` id → the candidates' `examples[0].kind == "email"`.
  - **TEACH tools:**
    - `propose_mapping` with `parser=GLOBEX_ROWS` and `{"Amount": {"field": "amount"}}` → `OK`.
    - The same call with a parser whose rows include the footer → `REFUSED ... skips_explained` or the cross-check.
  - **Web:** the source step offers Pages, Email, Files and Table. Email shows the folder and IMAP forms, with the
    password through the credentials drawer. The review shows "AI ✓ checked" and the cross-check numbers.
- [ ] **Step 2: Run and confirm they fail.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests and confirm they pass.** Run `.venv/bin/python -m pytest tests/control tests/agents -q && cd web && npm run -s typecheck && npx vitest run && npm run -s build`.
  Expected: PASS.
- [ ] **Step 5: Commit** with message `Wizard: choose where data comes from; TEACH from document examples with rows checks`.

### Task G15: Web: documents and their rows on the integration page

**Files:**
- Modify:
  - `web/src/components/Tracker.tsx`: group the Outcomes `<doc>#<n>` under their document. Show held documents with
    their reason and the cross-check numbers.
  - `control/api/server.py` `/api/tracker`: rows gain `document_id`, `row_no` and `document: {title, site, kind}`;
    additive.
  - `web/mock/v3.mjs`
- Test: `web/src/components/Tracker.test.tsx` (extend), `tests/control/test_api_tracker.py` (extend)

- [ ] **Step 1: Write the failing tests.**
  - `/api/tracker?integration=invoice_lines` after G11 returns 3 rows with `document_id == "doc-…"` and `row_no` 1–3.
  - The web groups them under one document header that says "learned by AI ✓ checked".
  - A held document shows `sum_equals: rows sum to 3,205.00 but subtotal is 3,215.00`.
- [ ] **Step 2: Run, implement, then run again.** Run `.venv/bin/python -m pytest tests/control/test_api_tracker.py -q && cd web && npx vitest run src/components/Tracker.test.tsx && npm run -s build`.
  Expected: PASS.
- [ ] **Step 3: Commit** with message `Web: documents with their rows and checks on the integration page`.

## Order, parallelism and files shared with plan A

**Effort (wall clock, with the lanes below; the teammate's readers and fixtures are in progress in parallel):**
- Phase 1: about 5 h.
- Phase 2 (G8): about 0.5 h once `read_pdf` lands.
- Phase 3 (G9–G11): about 2.5 h, plus 1.5 h for the demo (G12).
- Phase 4: about 2 h, optional.
- The wizard: about 2 h, after plan A.

**Lanes:**
- **Runner:** G1 → G2 → G3.
- **Documents:** G4 starts at once. Its tests build the teammate's `Document` directly until `read_any` lands.
  - G8 starts after G2, G3, G4 and the teammate's `read_pdf`.
  - G9 starts after G4 and the teammate's `read_eml` + `fixtures/emails/`.
  - G10 starts after G4. It uses `runner.imap.fetch_new` when it lands, and a fake `fetch` in tests.
- **Agents:** G5 starts after G2 and G4. G6 follows G5.
- **Joins:**
  - G7 needs G3, G4 and G6. **Phase 1 is done here.**
  - G11 needs G7, G8, G9 and G10.
  - G12 needs G11.
- **Optional, any time after G10:** G13.
- **After plan A:** G14 needs plan A Tasks 2, 4, 5, 7 and 11 and G6. G15 needs plan A Task 10 and G3.

**Can run in parallel:** {G1→G2→G3} ∥ {G4 → (G9 ∥ G10)} ∥ the teammate's readers and fixtures; then G5 → G6 and
G8; and G13 ∥ G12.

**Teammate, in progress (consumed only):** `runner/documents.py`, `runner/imap.py`, `fixtures/emails/` and
`scripts/make_email_fixtures.py`. No task here edits them.

**Files shared with plan A. Coordinate these merges and land plan A's change first where both edit:**

| File | Plan A task | This plan |
|---|---|---|
| `agents/deep_tools.py` | 5, 6, 8 | G4 (`get_page_record` dispatch), G5 (`check_candidate`, which plan A's tools must call), G6 (`TOOL_DOCS`), G14 |
| `agents/scripted_v2.py` | 7 | G6, G14 |
| `agents/prompts.py`, `agents/deep.py` | 6 | G6 |
| `agents/interface.py` | 5 | G5 (an optional `rows` on `evidence`) |
| `control/api/server.py` | 2, 3, 5 | G4, G10, G15 (routers and additive fields only) |
| `control/autorun.py`, `control/page_hooks.py` | 2 | G4 |
| `control/drafts.py`, `control/api/drafts.py` | 2, 4 | G14 |
| `scripts/smoke_v2.py` | 9 | G12 |
| `docs/demo-v2.md`, `web/src/components/Presenter.tsx` | 12 | G12 |
| `web/src/pages/NewIntegration.tsx`, `web/src/components/ReviewTable.tsx`, `web/src/api.ts`, `web/mock/v3.mjs` | 11 | G14, G15 |

**Cut order if time runs short:** G13, then G15, then G14. The demo stands on G1–G12; the integration is set up from
the fixture spec, without the wizard.
