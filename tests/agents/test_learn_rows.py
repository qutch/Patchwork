"""G5 (contracts V17, V18): LEARN on documents with rows. The helpers in agents/learn.py directly, and the learn
tools (`get_page`, `test_page_parser`, `save_parser`) through a harness that stands in for the engine, as in
test_learn_tools.py. No model, no network."""

from __future__ import annotations

import copy
import json
import threading
import time

import pytest
import yaml

from agents import learn
from agents.deep_tools import check_candidate
from runner.rows import Row
from runner.store import Store
from tests.agents.jobs import NAME, FakeEngine, active_spec, set_learn_policy

HEADER = ["Item", "Qty", "Each", "Line"]
BODY = [["Bolt", "2", "$1.50", "$3.00"], ["Nut", "4", "$0.25", "$1.00"]]
FOOTER = ["", "", "Sum", "$4.00"]
TEXT = "Ref R-9\nItem Qty Each Line\nBolt 2 $1.50 $3.00\nNut 4 $0.25 $1.00\nSum $4.00"
HTML = "<p>Ref R-9</p><table><tr><th>Item</th></tr></table>"

DOC_FIELDS = {"ref": {"regex": r"Ref\s+(\S+)"}, "total": {"regex": r"Sum\s+(\S+)", "clean": "money"}}
ROWS = {"select": {"table": {"columns": ["Item", "Qty"]}},
        "fields": {"item": {"column": "Item"}, "qty": {"column": "Qty", "cast": "int"},
                   "line": {"column": "Line", "clean": "money"}}}
GOOD = {"id": "south", "fields": DOC_FIELDS, "rows": ROWS}
NORTH = {"id": "north", "match": {"site": "north.example"}, "fields": DOC_FIELDS, "rows": ROWS,
         "origin": {"kind": "fixture", "at": None, "thread_id": None, "approved_by": None}}


def doc(site: str = "south.example", *, total: str = "$4.00", rows=None, doc_id: str = "doc-000009") -> dict:
    body = rows if rows is not None else BODY
    footer = [*FOOTER[:3], total]
    return {"id": doc_id, "parent_id": None, "kind": "html", "name": "o.html", "site": site, "integration": NAME,
            "text": TEXT.replace("$4.00", total), "html": HTML, "flags": [], "sha256": "h",
            "meta": {"from": f"a@{site}", "subject": "Order R-9"}, "received_at": "2026-11-14T00:00:00+00:00",
            "received_date": "2026-11-14", "is_leaf": True,
            "tables": [{"header": HEADER, "rows": [*body, footer], "where": "table 1"}]}


def rows_spec(*, cross_check: bool = True) -> dict:
    validate = [{"field": "ref", "rule": "required"}]
    if cross_check:
        validate.append({"rule": "sum_equals", "rows_field": "line", "field": "total"})
    return {
        "spec_version": 1, "name": NAME, "intent": "Copy each row of a table into the lines form",
        "source": {"kind": "api", "base_url": "patchwork://control",
                   "list": {"method": "GET", "path": "/api/documents", "params": {"since": "{cursor}"}},
                   "record_id": "id", "text_field": "text", "html_field": "html", "cursor_field": "seq"},
        "extract": {"route_by": "site", "parsers": [copy.deepcopy(NORTH)]},
        "validate": validate,
        "dedupe": {"key": ["ref", "record.row_no"]},
        "sink": {"kind": "form", "url": "http://127.0.0.1:9/", "submit": "Submit",
                 "fields": {"Ref": {"from": "ref"}, "Item": {"from": "item"}, "Qty": {"from": "qty"},
                            "Line": {"from": "line"}, "N": {"from": "record.row_no"}},
                 "verify": {"confirmation_text": "ok"}},
        "on_error": "quarantine"}


class DocHarness:
    def __init__(self, monkeypatch, rec: dict | None = None, *, cross_check: bool = True):
        from agents.deep_tools import ToolContext, seed_learn_workdir, tool_functions
        from agents.interface import _Thread
        from agents.scripted import ScriptedAgentService

        self.engine = FakeEngine().install(monkeypatch)
        self.rec = self.engine.add_page(rec or doc())
        self.engine.deploy_baseline(rows_spec(cross_check=cross_check), [])
        self.svc = ScriptedAgentService(pace_s=0)
        self.t = _Thread(id="learn-test0002", mode="learn", subject=self.rec["id"], integration=NAME)
        self.svc._threads[self.t.id] = self.t
        seed_learn_workdir(NAME)
        self.ctx = ToolContext(self.svc, self.t, name=NAME, page_id=self.rec["id"])
        self.tools = tool_functions(self.ctx)

    def events(self, type_=None):
        return [e for e in self.svc.events(self.t.id) if type_ is None or e["type"] == type_]

    def call_async(self, tool, **kw):
        box: dict = {}
        th = threading.Thread(target=lambda: box.setdefault("out", self.tools[tool](**kw)), daemon=True)
        th.start()
        return th, box

    def wait_pending(self, th, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.t.pending is not None:
                return self.t.pending
            if not th.is_alive():
                return None
            time.sleep(0.02)
        raise AssertionError("no interrupt")

    def answer(self, payload):
        self.svc.resume(self.t.id, self.t.pending["interrupt_id"], payload)


@pytest.fixture
def learn_ctx_doc(workspace, monkeypatch):
    set_learn_policy("auto")
    return DocHarness(monkeypatch)


# ---- the helpers --------------------------------------------------------------------------------------

def test_extract_field_matches_the_compiled_parser_order_and_clean():
    rec = {"id": "x", "text": "Total: 1,299.00 EUR", "title": "Ref 7 and more"}
    assert learn.extract_field({"regex": r"Total:\s*(\S+)", "clean": "money"}, rec, "w") == 1299.0
    # a regex next to json_path is a refine of the JSON value, not a primary regex on the text
    assert learn.extract_field({"json_path": "$.title", "regex": r"Ref\s+(\d+)", "cast": "int"}, rec, "w") == 7
    assert learn.extract_field({"regex": r"Total:\s*(\S+)", "clean": "money", "cast": "float"}, rec, "w") == 1299.0


def test_document_outline_shows_meta_tables_and_numbered_lines():
    out = learn.document_outline(doc())
    assert "document doc-000009" in out and "south.example" in out and 'subject: "Order R-9"' in out
    assert 'T1 columns: ["Item", "Qty", "Each", "Line"] rows: 3' in out and '"Bolt"' in out
    assert "L2: Item Qty Each Line" in out and len(out) <= 4000
    assert len(learn.document_outline({**doc(), "text": "x\n" * 5000}, max_chars=900)) <= 900


def test_document_outline_names_flags():
    assert "flag: no_text_layer" in learn.document_outline({**doc(), "flags": ["no_text_layer"]})


def test_evidence_accepts_table_cells_meta_and_cleaned_forms():
    rec = {**doc(), "text": ""}  # nothing in the text: a value can only be evidenced by a table cell or meta
    ev = {i["name"]: i for i in learn.evidence(rec, {"x": "Bolt", "s": "Order R-9", "n": 4.0},
                                               [], parser={"fields": {}})}
    assert (ev["x"]["where"], ev["s"]["where"], ev["n"]["ok"]) == ("table", "meta", True)
    assert learn.evidence(rec, {"x": "Washer"}, [], parser={"fields": {}})[0]["detail"] == "value does not occur in the document"
    d = {**doc(), "text": "Issued 03 Nov 2026"}
    spec = {"fields": {"d": {"regex": r"Issued\s+(.+)", "clean": {"date": ["%d %b %Y"]}}}}
    assert learn.evidence(d, {"d": "2026-11-03"}, [], parser=spec)[0]["ok"] is True
    assert learn.evidence(d, {"d": "2026-11-04"}, [], parser=spec)[0]["ok"] is False


def test_page_evidence_is_unchanged_by_the_document_forms():
    page = {"id": "pg-000001", "text": "Total 1,299.00", "html": "<p>Total 1,299.00</p>", "title": ""}
    assert learn.evidence(page, {"t": 1299.0}, [], parser={"fields": {}})[0]["ok"] is False  # a page needs the literal text
    document = {**doc(), "text": "Total 1,299.00", "tables": []}
    assert learn.evidence(document, {"t": 1299.0}, [], parser={"fields": {}})[0]["ok"] is True  # a document may be cleaned


def test_rows_checks_pass_on_the_good_parser():
    out = learn.run_document_parser(GOOD, doc())
    assert [r.fields["item"] for r in out["rows"]] == ["Bolt", "Nut"] and out["skipped"][0]["reason"] == "aggregate"
    checks = learn.rows_checks(doc(), GOOD, out["rows"], rows_spec()["validate"], skipped=out["skipped"],
                               doc_fields=out["fields"])
    assert {c["name"]: c["ok"] for c in checks} == {
        "values_in_own_row": True, "required_present": True, "no_duplicate_rows": True, "skips_explained": True,
        "row_count": True, "rules_hold": True, "sum_equals": True}
    assert next(c for c in checks if c["name"] == "sum_equals").get("cross") is True


def test_values_in_own_row_catches_a_value_from_another_row():
    rows = learn.run_document_parser(GOOD, doc())["rows"]
    rows[1] = Row(rows[1].no, rows[1].text, rows[1].cells, {**rows[1].fields, "item": "Bolt"})  # row 2 reads row 1
    bad = next(c for c in learn.rows_checks(doc(), GOOD, rows, [], doc_fields={}) if c["name"] == "values_in_own_row")
    assert bad["ok"] is False and "row 2" in bad["detail"] and "item='Bolt'" in bad["detail"]


def test_required_present_needs_95_percent_of_rows():
    rows = [Row(i, f"r{i}", {}, {"item": f"i{i}", "qty": 1, "line": "" if i > 1 else 1.0}) for i in range(1, 5)]
    parser = {"id": "p", "fields": {}, "rows": {"select": {"css": "tr"}, "fields": {
        "item": {"column": "a"}, "qty": {"column": "b"}, "line": {"column": "c"}}}}
    out = {c["name"]: c for c in learn.rows_checks({"text": ""}, parser, rows, [{"field": "line", "rule": "required"}],
                                                   skipped=[], doc_fields={})}
    assert out["required_present"]["ok"] is False and "line is empty in 3 of 4 rows" in out["required_present"]["detail"]


def test_duplicate_rows_and_a_failed_row_rule_are_reported():
    r = lambda n, **f: Row(n, f"t{n}", {}, {"item": "a", "qty": 1, **f})  # noqa: E731
    parser = {"id": "p", "fields": {}, "rows": {"select": {"css": "tr"}, "fields": {"item": {"column": "a"}, "qty": {"column": "b"}}}}
    same = {c["name"]: c for c in learn.rows_checks({"text": ""}, parser, [r(1), r(2)], [], skipped=[], doc_fields={})}
    assert same["no_duplicate_rows"]["ok"] is False and "rows 1 and 2" in same["no_duplicate_rows"]["detail"]
    rule = [{"field": "qty", "rule": "positive_int"}]
    held = {c["name"]: c for c in learn.rows_checks({"text": ""}, parser, [r(1, qty=0)], rule, skipped=[], doc_fields={})}
    assert held["rules_hold"]["ok"] is False and "row 1" in held["rules_hold"]["detail"]


def test_check_candidate_on_a_document_and_a_page():
    spec = rows_spec()
    res = check_candidate(GOOD, doc(), spec)
    assert res["rows_ok"] and res["evidence_ok"] and res["cross_checks_passed"] == 1 and len(res["rows"]) == 2
    assert res["all_corroborated"] is False  # a document has no title or meta tags to corroborate it
    bad = check_candidate({**GOOD, "rows": {**ROWS, "require": ["line"]}}, doc(), spec)  # the footer fills `Line`
    assert "error" in bad and "adds up the other rows" in bad["error"]
    page = check_candidate({"id": "p", "fields": {"t": {"css": "h1"}}}, {"id": "pg-000001", "html": "<h1>Hi</h1>",
                           "text": "Hi", "title": ""}, {"source": {"html_field": "html", "text_field": "text"}})
    assert page["rows"] == [] and page["rows_checks"] == [] and page["rows_ok"] is True and page["cross_checks_passed"] == 0


def test_a_document_regex_that_matches_twice_differently_is_ambiguous():
    rec = {**doc(), "text": "Ref R-9\nRef R-10"}
    assert learn.ambiguity(rec, {"fields": {"ref": {"regex": r"Ref\s+(\S+)"}}}) == {"ref": ["R-9", "R-10"]}
    assert learn.ambiguity({**rec, "text": "Ref R-9\nRef R-9"}, {"fields": {"ref": {"regex": r"Ref\s+(\S+)"}}}) == {}
    page = {"id": "pg-000001", "text": "Ref R-9\nRef R-10", "html": ""}  # pages keep their old behaviour
    assert learn.ambiguity(page, {"fields": {"ref": {"regex": r"Ref\s+(\S+)"}}}) == {}


def test_d2_documents_need_a_passing_cross_check_in_place_of_corroboration():
    common = dict(policy="auto", tests_passed=True, evidence_ok=True, ambiguous=None, additive=True)
    assert learn.d2_decision(**common, all_corroborated=False, is_document=True, cross_checks_passed=1) == (True, [])
    auto, why = learn.d2_decision(**common, all_corroborated=True, is_document=True, cross_checks_passed=0)
    assert auto is False and "no declared cross-check" in why[0]
    assert learn.d2_decision(**common, all_corroborated=False)[0] is False  # a page still needs corroboration


# ---- the tools ----------------------------------------------------------------------------------------

def test_get_page_outlines_the_document_and_the_rows_contract(learn_ctx_doc):
    out = learn_ctx_doc.tools["get_page"]()
    assert 'T1 columns: ["Item", "Qty", "Each", "Line"]' in out and "L1: Ref R-9" in out
    assert "fields to extract (exactly these): ref, total" in out
    assert "row fields (exactly these, one value per row): item, qty, line" in out
    assert "select: {table:" in out and "how existing parsers select rows: north:" in out


def test_good_rows_parser_passes_every_check(learn_ctx_doc):
    out = learn_ctx_doc.tools["test_page_parser"](parser=GOOD)
    assert out.startswith("OK"), out
    rows = json.loads(out[len("OK: "):out.index("\nnext:")])["rows"]
    assert rows["count"] == 2 and all(rows["checks"].values()) and rows["checks"]["sum_equals"] is True
    assert rows["first"][0] == {"item": "Bolt", "qty": 2, "line": 3.0} and rows["skipped"][0]["reason"] == "aggregate"
    ev = learn_ctx_doc.events("evidence")[-1]
    assert ev["ok"] and ev["rows"]["count"] == 2 and {c["name"] for c in ev["rows"]["checks"]} >= {"sum_equals", "row_count"}
    assert {f["name"]: f["where"] for f in ev["fields"]} == {"ref": "text", "total": "text"}


def test_footer_kept_as_a_row_is_refused(learn_ctx_doc):
    out = learn_ctx_doc.tools["save_parser"](parser={**GOOD, "rows": {**ROWS, "require": ["line"]}}, reason="x")
    assert out.startswith("REFUSED") and "adds up the other rows" in out
    assert Store().active_version(NAME) == 1


def test_a_footer_that_does_not_add_up_holds_the_document_before_any_check(workspace, monkeypatch):
    set_learn_policy("auto")
    h = DocHarness(monkeypatch, doc(total="$5.00"))  # the table's own footer says 5.00 but its rows add up to 4.00
    out = h.tools["save_parser"](parser=GOOD, reason="x")
    assert out.startswith("REFUSED: the parser does not read this page") and "skipped but not blank" in out
    assert Store().active_version(NAME) == 1


def test_a_cross_check_that_fails_on_the_document_is_refused(workspace, monkeypatch):
    set_learn_policy("auto")
    rec = {**doc(), "text": TEXT + "\nTotal due $5.00"}  # an explained footer (4.00), but the declared total says 5.00
    h = DocHarness(monkeypatch, rec)
    due = {**GOOD, "fields": {**DOC_FIELDS, "total": {"regex": r"Total due\s+(\S+)", "clean": "money"}}}
    out = h.tools["save_parser"](parser=due, reason="x")
    assert out.startswith("REFUSED: rows check sum_equals failed") and "4.00" in out and "5.00" in out
    assert Store().active_version(NAME) == 1 and h.events("learned") == []
    ok = h.tools["test_page_parser"](parser=due)
    assert ok.startswith("FAILED") and '"sum_equals": "' in ok  # the report names the failing check and why


def test_wrong_column_mapping_is_refused(learn_ctx_doc):
    wrong = {**GOOD, "rows": {**ROWS, "fields": {**ROWS["fields"], "qty": {"column": "Each", "cast": "int"}}}}
    out = learn_ctx_doc.tools["save_parser"](parser=wrong, reason="x")
    assert out.startswith("REFUSED") and Store().active_version(NAME) == 1


def test_parser_without_rows_or_with_other_row_fields_is_refused(learn_ctx_doc):
    assert "needs `rows`" in learn_ctx_doc.tools["save_parser"](parser={"id": "s", "fields": DOC_FIELDS})
    other = {**GOOD, "rows": {**ROWS, "fields": {"item": ROWS["fields"]["item"]}}}
    assert "must be exactly the integration's row fields" in learn_ctx_doc.tools["save_parser"](parser=other)


def test_save_parser_auto_deploys_with_a_passing_cross_check_and_per_row_golden(learn_ctx_doc):
    h = learn_ctx_doc
    out = h.tools["save_parser"](parser=GOOD, reason="new sender")
    assert out.startswith("deployed v2 automatically"), out
    assert Store().active_version(NAME) == 2
    p = next(x for x in active_spec()["extract"]["parsers"] if x["id"] == "south")
    assert p["match"] == {"site": "south.example"} and p["origin"]["approved_by"] == "auto" and p["rows"]["select"]
    learned = h.events("learned")[-1]
    assert learned["approval"] == "auto" and learned["deployed"] is True and "cross-check" in learned["reason"]
    gold = yaml.safe_load((h.ctx.wd / "examples.yaml").read_text())
    ex = next(e for e in gold if e["name"].startswith("learned_south"))
    assert [b["Item"] for b in ex["expect"]["rows"]] == ["Bolt", "Nut"]
    assert [b["N"] for b in ex["expect"]["rows"]] == ["1", "2"] and ex["expect"]["rows"][0]["Line"] == "3.0"
    assert ex["record"]["id"] == "doc-000009" and "body" not in ex["expect"]


def test_auto_deploy_needs_a_passing_cross_check(workspace, monkeypatch):
    set_learn_policy("auto")
    h = DocHarness(monkeypatch, cross_check=False)  # a layout with no total to check
    th, box = h.call_async("save_parser", parser=GOOD, reason="new sender")
    req = h.wait_pending(th)
    assert req["type"] == "request_deploy" and "no declared cross-check" in req["summary"]
    assert Store().active_version(NAME) == 1 and h.events("learned")[-1]["approval"] == "pending"
    h.answer({"decision": "reject", "comment": "no total to check"})
    th.join(10)
    assert box["out"].startswith("REJECTED") and h.events("learned")[-1]["approval"] == "human"


def test_a_page_learn_still_deploys_without_a_cross_check(workspace, monkeypatch):
    """The V10 rule for pages is unchanged: corroboration, not a cross-check."""
    from tests.agents.test_learn_tools import Harness
    from tests.agents.jobs import READERS

    set_learn_policy("auto")
    h = Harness(monkeypatch, "greenhouse-1")
    out = h.tools["save_parser"](parser={"id": "greenhouse", "fields": READERS["greenhouse"]}, reason="new site")
    assert out.startswith("deployed v2 automatically")


# ---- a page whose integration reads rows is judged like a document ----------------------------------------

def test_a_cleaned_value_on_a_page_is_evidenced_by_its_forms():
    page = {"id": "pg-000001", "text": "Total 1,299.00", "html": "<p>Total 1,299.00</p>", "title": ""}
    spec = {"fields": {"t": {"regex": r"Total\s+(\S+)", "clean": "money"}}}
    assert learn.evidence(page, {"t": 1299.0}, [], parser=spec)[0]["ok"] is True
    assert learn.evidence(page, {"t": 1299.0}, [], parser={"fields": {"t": {"regex": r"Total\s+(\S+)"}}})[0]["ok"] is False


def test_get_page_shows_text_lines_for_a_page_when_the_integration_reads_rows(workspace, monkeypatch):
    page = doc(doc_id="pg-000003")
    for key in ("tables", "kind", "meta"):
        page.pop(key)  # a page record has none of these
    h = DocHarness(monkeypatch, page)
    out = h.tools["get_page"]()
    assert "kind page" in out and "L2: Item Qty Each Line" in out and "row fields (exactly these" in out
