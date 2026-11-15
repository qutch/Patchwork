"""G3: one document -> many records (contracts V18): compiled rows parsers, process_document, the run loop and
replays. Documents carry their tables as V17 dicts (record["tables"]), so nothing here needs runner.documents."""

from __future__ import annotations

import copy
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi import FastAPI
from selectolax.lexbor import LexborHTMLParser

from compiler.compile import Example, compile_spec
from runner.html import visible_text
from runner.loader import load_integration
from runner.pipeline import MemoryStore, Outcome, process_document
from runner.store import Store
from runner.testing import FakeHttp, FakeResponse
from spec import load_spec, parse_spec
from tests.conftest import _Server

REPO = Path(__file__).resolve().parents[2]
ROWS_SPEC = Path(__file__).resolve().parent / "data" / "rows_spec.yaml"
TABLE = ("<table><tr><th>Item</th><th>Qty</th><th>Each</th><th>Line</th></tr>"
         "<tr><td>Bolt</td><td>2</td><td>$1.50</td><td>$3.00</td></tr>"
         "<tr><td>Nut</td><td>4</td><td>$0.25</td><td>$1.00</td></tr>"
         "<tr><td></td><td></td><td>Sum</td><td>$4.00</td></tr></table>")
BODIES = [{"Ref": "R-1", "Row": 1, "Of": 2, "Item": "Bolt", "Qty": 2, "Line": 3.0},
          {"Ref": "R-1", "Row": 2, "Of": 2, "Item": "Nut", "Qty": 4, "Line": 1.0}]


def tables_of(html: str) -> list[dict]:
    """Test-only stand-in for runner.documents.read_html(...).tables, as V17 dicts."""
    out = []
    for i, t in enumerate(LexborHTMLParser(html).css("table"), 1):
        trs = [[" ".join(c.text().split()) for c in tr.css("th,td")] for tr in t.css("tr")]
        out.append({"header": trs[0], "rows": trs[1:], "where": f"table {i}"})
    return out


def doc(html: str, n: int = 1, ref: str = "R-1") -> dict:
    return {"id": f"doc-{n:06d}", "seq": n, "kind": "html", "site": "shop.example", "name": f"{ref}.html",
            "html": html, "text": visible_text(html), "tables": tables_of(html) if html else [],
            "meta": {"subject": f"Your table Ref {ref}"}, "flags": []}


def _raw_spec(**changes) -> dict:
    raw = yaml.safe_load(ROWS_SPEC.read_text())
    for rule in changes.get("extra_rules", []):
        raw["validate"].append(rule)
    if "hold" in changes:
        raw["extract"]["parsers"][0]["rows"]["hold"] = changes["hold"]
    return raw


def _compile(raw: dict, out_root: Path, examples: list[Example] | None = None, sink_openapi: dict | None = None):
    res = compile_spec(parse_spec(raw), version=1, out_root=out_root, examples=examples, sink_openapi=sink_openapi)
    return res, load_integration(res.path)


def _examples() -> list[Example]:
    return [Example.parse({"name": "two-lines", "record": doc(TABLE), "expect": {"rows": BODIES}}),
            Example.parse({"name": "bad-sum", "record": doc(TABLE.replace("$4.00", "$5.00")),
                           "expect": {"quarantine": "VALIDATION_FAILED"}})]


@pytest.fixture(scope="module")
def rows_compiled(tmp_path_factory):
    return _compile(_raw_spec(), tmp_path_factory.mktemp("integrations"), _examples())


@pytest.fixture(scope="module")
def rows_pkg(rows_compiled):
    return rows_compiled[1]


@pytest.fixture(scope="module")
def rows_pkg_qty_positive(tmp_path_factory):
    raw = _raw_spec(extra_rules=[{"field": "qty", "rule": "positive_int"}])
    return _compile(raw, tmp_path_factory.mktemp("integrations"))[1]


@pytest.fixture
def fake_http():
    return FakeHttp()


def compile_fixture(path: str, tmp_path: Path) -> SimpleNamespace:
    res = compile_spec(load_spec(REPO / path), version=1, out_root=tmp_path / Path(path).parent.name)
    return SimpleNamespace(out_dir=res.path, result=res)


# ---- the brief's cases ------------------------------------------------------------------------------------

def test_one_document_many_outcomes(rows_pkg, fake_http):
    outs = process_document(rows_pkg, doc(TABLE), fake_http, MemoryStore())
    assert [o.record_id for o in outs] == ["doc-000001#1", "doc-000001#2"] and {o.status for o in outs} == {"success"}
    assert [b["Line"] for b in fake_http.sink_writes] == [3.0, 1.0]
    assert [o.body for o in outs] == BODIES and {o.parser for o in outs} == {"shop"}
    assert [o.key for o in outs] == ["R-1|1", "R-1|2"]


def test_failed_cross_check_holds_document_writes_nothing(rows_pkg, fake_http):
    outs = process_document(rows_pkg, doc(TABLE.replace("$4.00", "$5.00")), fake_http, MemoryStore())
    assert [(o.record_id, o.reason) for o in outs] == [("doc-000001", "VALIDATION_FAILED")]
    assert "sum_equals" in outs[0].detail and "5.00" in outs[0].detail and fake_http.sink_writes == []
    # the footer no longer adds up, so it is also an unexplained row; the failed cross-check is named first
    assert outs[0].detail.startswith("sum_equals: the rows' line add up to 4.00 but total is 5.00; and shop.rows: "
                                     "row 3 skipped but not blank")


def test_failed_cross_check_with_the_total_outside_the_table(rows_pkg, fake_http):
    html = TABLE.replace("<tr><td></td><td></td><td>Sum</td><td>$4.00</td></tr>", "") + "<p>Sum $3,215.00</p>"
    out, = process_document(rows_pkg, doc(html), fake_http, MemoryStore())
    assert (out.record_id, out.reason) == ("doc-000001", "VALIDATION_FAILED") and fake_http.sink_writes == []
    assert out.detail == "sum_equals: the rows' line add up to 4.00 but total is 3,215.00"


def test_hold_document_validates_every_row_before_any_write(rows_pkg_qty_positive, fake_http):
    outs = process_document(rows_pkg_qty_positive, doc(TABLE.replace("<td>4</td>", "<td>-4</td>")), fake_http, MemoryStore())
    assert len(outs) == 1 and outs[0].reason == "VALIDATION_FAILED" and fake_http.sink_writes == []
    assert outs[0].record_id == "doc-000001" and "row 2" in outs[0].detail and "qty" in outs[0].detail


def test_replay_after_partial_success_writes_no_duplicates(rows_pkg, fake_http):
    store = MemoryStore()
    process_document(rows_pkg, doc(TABLE), fake_http, store)
    again = process_document(rows_pkg, doc(TABLE), fake_http, store)
    assert {o.status for o in again} == {"duplicate"} and len(fake_http.sink_writes) == 2


def test_flagged_document_is_held_with_plain_reason(rows_pkg, fake_http):
    out, = process_document(rows_pkg, {**doc(""), "flags": ["no_text_layer"]}, fake_http, MemoryStore())
    assert out.reason == "PARSE_NO_MATCH" and "no text layer" in out.detail
    assert out.record_id == "doc-000001" and fake_http.sink_writes == []


def test_v1_and_jobs_packages_have_no_rows_code(tmp_path):
    for p in ("fixtures/spec.yaml", "fixtures/jobs/spec.yaml"):
        res = compile_fixture(p, tmp_path)
        assert "extract_rows" not in (res.out_dir / "parsers.py").read_text() and "EMITS" not in (res.out_dir / "integration.py").read_text()


# ---- more of process_document -----------------------------------------------------------------------------

def test_other_flags_are_held(rows_pkg, fake_http):
    for flag, words in (("encrypted", "password"), ("unreadable", "could not be read")):
        out, = process_document(rows_pkg, {**doc(TABLE), "flags": [flag]}, fake_http, MemoryStore())
        assert (out.status, out.reason) == ("quarantined", "PARSE_NO_MATCH") and words in out.detail
    assert fake_http.sink_writes == []


def test_unrouted_and_unparsed_documents_give_one_outcome(rows_pkg, fake_http):
    out, = process_document(rows_pkg, {**doc(TABLE), "site": "elsewhere.example"}, fake_http, MemoryStore())
    assert (out.record_id, out.reason) == ("doc-000001", "PARSE_NO_MATCH") and "elsewhere.example" in out.detail
    out, = process_document(rows_pkg, {**doc(TABLE), "tables": []}, fake_http, MemoryStore())
    assert out.reason == "PARSE_NO_MATCH" and "no table has the columns" in out.detail and out.routed_by == "match"
    bad_footer = TABLE.replace("<td></td><td></td><td>Sum</td>", "<td></td><td>9</td><td>Sum</td>")
    out, = process_document(rows_pkg, doc(bad_footer), fake_http, MemoryStore())
    assert out.reason == "PARSE_NO_MATCH" and "row 3 skipped but not blank" in out.detail
    assert fake_http.sink_writes == []


def test_hold_row_holds_only_the_bad_row(tmp_path, fake_http):
    raw = _raw_spec(hold="row", extra_rules=[{"field": "qty", "rule": "positive_int"}])
    _, integ = _compile(raw, tmp_path)
    outs = process_document(integ, doc(TABLE.replace("<td>4</td>", "<td>-4</td>")), fake_http, MemoryStore())
    assert [(o.record_id, o.status, o.reason) for o in outs] == [
        ("doc-000001#1", "success", None), ("doc-000001#2", "quarantined", "VALIDATION_FAILED")]
    assert [b["Item"] for b in fake_http.sink_writes] == ["Bolt"]


def test_rows_with_the_same_dedupe_key_hold_the_document(tmp_path, fake_http):
    raw = _raw_spec()
    raw["dedupe"] = {"key": ["ref"]}  # does not tell the rows apart
    _, integ = _compile(raw, tmp_path)
    out, = process_document(integ, doc(TABLE), fake_http, MemoryStore())
    assert out.reason == "VALIDATION_FAILED" and "same dedupe key" in out.detail and fake_http.sink_writes == []


def test_sink_rejection_is_per_row(rows_pkg):
    http = FakeHttp(sink_status=422)
    outs = process_document(rows_pkg, doc(TABLE), http, MemoryStore())
    assert [(o.record_id, o.reason) for o in outs] == [("doc-000001#1", "SINK_CONTRACT"), ("doc-000001#2", "SINK_CONTRACT")]


def test_package_shape(rows_compiled):
    res, integ = rows_compiled
    assert integ.EMITS == "rows" and integ.HOLDS == {"shop": "document"}
    parsers = (res.path / "parsers.py").read_text()
    assert "ROWS_SHOP = {" in parsers and "extract_rows(record, ROWS_SHOP, 'shop.rows', HTML_FIELD, TEXT_FIELD)" in parsers
    parser_id, fields = integ.extract(doc(TABLE))
    assert parser_id == "shop" and fields["ref"] == "R-1" and fields["total"] == 4.0
    assert [r.fields["item"] for r in fields["__rows__"]] == ["Bolt", "Nut"]
    integ.validate_document({"total": 4.0}, fields["__rows__"])
    golden = json.loads((res.path / "golden.json").read_text())
    assert golden[0]["expect"] == {"rows": BODIES}


def test_generated_tests_pass(rows_compiled):
    res, _ = rows_compiled
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(res.path / "tests")],
                       capture_output=True, text=True, cwd=REPO)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "skipped" in r.stdout or "passed" in r.stdout


def test_example_expect_shapes():
    from compiler.compile import CompileError
    with pytest.raises(CompileError, match="exactly one of body/quarantine/rows"):
        Example.parse({"name": "x", "record": {}, "expect": {"rows": [], "body": {}}})
    with pytest.raises(CompileError, match="rows must be a list of bodies"):
        Example.parse({"name": "x", "record": {}, "expect": {"rows": {"a": 1}}})


# ---- the run loop and replays -------------------------------------------------------------------------------

LINES_DDL = 'CREATE TABLE lines ("Ref" TEXT, "Row" INTEGER, "Of" INTEGER, "Item" TEXT, "Qty" INTEGER, "Line" REAL)'


def test_run_and_replay_end_to_end(workspace, monkeypatch):
    from runner import sqlite_sink
    from runner.run import run

    db = workspace / "lines.db"
    with sqlite3.connect(db) as conn:
        conn.execute(LINES_DDL)
    contract = sqlite_sink.fetch_contract("lines.db", "lines")
    res = compile_spec(parse_spec(_raw_spec()), version=1, out_root=workspace / "integrations",
                       examples=_examples(), sink_openapi=contract)
    store = Store()
    store.add_version("table_lines", 1, res.spec_hash, res.openapi_hash)
    store.set_active("table_lines", 1)

    docs = [doc(TABLE, 1, "R-1"), doc(TABLE.replace("$4.00", "$5.00"), 2, "R-2")]
    app = FastAPI()

    @app.get("/api/documents")
    def documents(since: str = "", integration: str = ""):
        return [d for d in docs if d["seq"] > int(since or 0)]

    with _Server(app) as server:
        monkeypatch.setenv("PATCHWORK_CONTROL_URL", server.url)
        summary = run("table_lines", store=store, log=lambda _: None)
        assert summary["status"] == "completed", summary
        assert summary["counts"] == {"success": 2, "quarantined": 1, "quarantined:VALIDATION_FAILED": 1}
        records = store.records(summary["run_id"])
        assert [r["record_id"] for r in records] == ["doc-000001#1", "doc-000001#2", "doc-000002"]
        assert records[0]["raw"]["id"] == "doc-000001" and records[2]["raw"]["id"] == "doc-000002"
        assert store.get_cursor("table_lines", "live") == "2"
        with sqlite3.connect(db) as conn:
            assert conn.execute('SELECT "Ref", "Row", "Item", "Line" FROM lines').fetchall() == [
                ("R-1", 1, "Bolt", 3.0), ("R-1", 2, "Nut", 1.0)]

        assert [r["id"] for r in store.quarantined_raw("table_lines")] == ["doc-000002"]
        docs[1] = doc(TABLE, 2, "R-2")  # the held document, corrected, comes back through a replay
        store.db.execute("UPDATE records SET raw=? WHERE record_id='doc-000002'", (json.dumps(docs[1]),))
        replay = run("table_lines", replay=True, store=store, log=lambda _: None)
        assert replay["counts"] == {"success": 2}, replay
        assert store.quarantined_raw("table_lines") == []  # the old document-level hold is superseded
        again = run("table_lines", replay=True, store=store, log=lambda _: None)
        assert again["counts"] == {}
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM lines").fetchone() == (4,)


def test_quarantined_raw_replays_a_document_once(tmp_path):
    store = Store(tmp_path / "p.db")
    raw = {"id": "doc-000009", "seq": 9}
    run1 = store.start_run("x", 1, False)
    store.add_record(run1, Outcome("doc-000009#1", "success"), raw)
    store.add_record(run1, Outcome("doc-000009#2", "quarantined", "VALIDATION_FAILED"), raw)
    store.add_record(run1, Outcome("doc-000009#3", "quarantined", "VALIDATION_FAILED"), raw)
    v1 = {"id": "po-1"}
    store.add_record(run1, Outcome("po-1", "quarantined", "PARSE_NO_MATCH"), v1)
    store.add_record(run1, Outcome("po-2", "quarantined", "PARSE_NO_MATCH"), {"no_id": True})
    assert store.quarantined_raw("x") == [raw, v1, {"no_id": True}]
    run2 = store.start_run("x", 1, False)
    for n in (1, 2, 3):
        store.add_record(run2, Outcome(f"doc-000009#{n}", "duplicate" if n == 1 else "success"), raw)
    assert store.quarantined_raw("x") == [v1, {"no_id": True}]


# ---- fix round 1 ---------------------------------------------------------------------------------------------

class _FailSecondWrite(FakeHttp):
    """The sink refuses the second write it ever receives (a 422), then accepts everything."""

    def sqlite_submit(self, path, table, values):
        self.n = getattr(self, "n", 0) + 1
        if self.n == 2:
            self.calls.append(("sink", "POST", "/rows", dict(values)))
            return FakeResponse(422, {"error": "rejected once"})
        return super().sqlite_submit(path, table, values)


def test_partial_success_then_replay_writes_row_one_once(rows_pkg):
    """Minor 2 / review focus 4: row 2's write fails, the document is replayed, and row 1 is written once."""
    http, store = _FailSecondWrite(), MemoryStore()
    first = process_document(rows_pkg, doc(TABLE), http, store)
    assert [(o.record_id, o.status, o.reason) for o in first] == [
        ("doc-000001#1", "success", None), ("doc-000001#2", "quarantined", "SINK_CONTRACT")]
    again = process_document(rows_pkg, doc(TABLE), http, store)
    assert [(o.record_id, o.status) for o in again] == [("doc-000001#1", "duplicate"), ("doc-000001#2", "success")]
    assert [b["Item"] for b in http.rows] == ["Bolt", "Nut"]  # rows actually written: each exactly once


def test_quarantined_raw_groups_by_the_record_id_field(tmp_path):
    """Important 3: the document id field is the spec's source.record_id, not always `id`."""
    store = Store(tmp_path / "p.db")
    raw = {"doc_key": "K-9", "seq": 9}
    run1 = store.start_run("x", 1, False)
    store.add_record(run1, Outcome("K-9", "quarantined", "PARSE_NO_MATCH"), raw)  # an old document-level hold
    run2 = store.start_run("x", 1, False)
    store.add_record(run2, Outcome("K-9#1", "quarantined", "VALIDATION_FAILED"), raw)
    store.add_record(run2, Outcome("K-9#2", "quarantined", "VALIDATION_FAILED"), raw)
    assert store.quarantined_raw("x", id_field="doc_key") == [raw]
    run3 = store.start_run("x", 1, False)
    store.add_record(run3, Outcome("K-9#1", "success"), raw)
    store.add_record(run3, Outcome("K-9#2", "success"), raw)
    assert store.quarantined_raw("x", id_field="doc_key") == []  # the run-1 hold is superseded


def test_replay_with_a_record_id_other_than_id_processes_the_document_once(workspace, monkeypatch):
    from runner import sqlite_sink
    from runner.run import run

    with sqlite3.connect(workspace / "lines.db") as conn:
        conn.execute(LINES_DDL)
    raw_spec = _raw_spec(hold="row", extra_rules=[{"field": "qty", "rule": "positive_int"}])
    raw_spec["source"]["record_id"] = "doc_key"
    res = compile_spec(parse_spec(raw_spec), version=1, out_root=workspace / "integrations",
                       sink_openapi=sqlite_sink.fetch_contract("lines.db", "lines"))
    store = Store()
    store.add_version("table_lines", 1, res.spec_hash, res.openapi_hash)
    store.set_active("table_lines", 1)

    def keyed(html: str) -> dict:
        d = doc(html)
        d["doc_key"] = d.pop("id").replace("doc-", "key-")
        return d

    bad = TABLE.replace("<td>2</td>", "<td>-2</td>").replace("<td>4</td>", "<td>-4</td>")  # both rows held
    docs = [keyed(bad)]
    app = FastAPI()

    @app.get("/api/documents")
    def documents(since: str = "", integration: str = ""):
        return docs

    with _Server(app) as server:
        monkeypatch.setenv("PATCHWORK_CONTROL_URL", server.url)
        summary = run("table_lines", store=store, log=lambda _: None)
        assert summary["counts"] == {"quarantined": 2, "quarantined:VALIDATION_FAILED": 2}, summary
        assert [r["record_id"] for r in store.records(summary["run_id"])] == ["key-000001#1", "key-000001#2"]
        store.db.execute("UPDATE records SET raw=?", (json.dumps(keyed(TABLE)),))
        replay = run("table_lines", replay=True, store=store, log=lambda _: None)
        assert replay["counts"] == {"success": 2}, replay  # one pass over the document, not one per held row
