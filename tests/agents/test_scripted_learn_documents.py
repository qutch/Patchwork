"""G6 (contracts V17, V18): the scripted agent learns a document layout with no model, through the same tools and
checks, and emits the same events as the deep agent."""

from __future__ import annotations

import pytest

from agents.scripted import ScriptedAgentService
from runner.rows import extract_rows
from runner.store import Store
from tests.agents.jobs import FakeEngine, NAME, active_spec, set_learn_policy
from tests.agents.test_deep_agent import check_events, wait_for
from tests.agents.test_learn_rows import DocHarness, doc, rows_spec

PDF_TEXT = "Ref R-9\nItem Qty Each Line\nBolt 2 1.50 3.00\nNut 4 0.25 1.00\nSum 4.00"


def pdf_rec(text: str = PDF_TEXT) -> dict:
    return {"id": "doc-000009", "kind": "pdf", "site": "b.example", "name": "b.pdf", "tables": [], "html": "",
            "text": text, "meta": {"from": "x@b.example"}, "flags": [], "integration": NAME}


@pytest.fixture
def scripted(workspace):
    return ScriptedAgentService(pace_s=0)


def test_scripted_proposes_a_table_parser_with_money_and_int_columns(scripted):
    p = scripted.propose_rows_parser(doc(), rows_spec())
    assert p["id"] == "south" and p["rows"]["select"] == {"table": {"columns": ["Item", "Qty", "Line"]}}
    assert p["rows"]["fields"] == {"item": {"column": "Item"}, "qty": {"column": "Qty", "cast": "int"},
                                   "line": {"column": "Line", "clean": "money"}}
    assert p["rows"]["require"] == ["item"]
    assert p["fields"]["total"] == {"regex": r"Sum\s+(\S+)", "clean": "money"}  # the existing reader, reused
    assert p["fields"]["ref"] == {"regex": r"Ref\s+(\S+)"}  # an existing parser's reader is tried first
    assert [r.fields["line"] for r in extract_rows(doc(), p["rows"], "w")] == [3.0, 1.0]


def test_scripted_falls_back_to_a_label_regex_when_the_existing_reader_misses(scripted):
    rec = {**doc(), "text": "REF: R-9\nItem Qty Each Line\nBolt 2 $1.50 $3.00\nNut 4 $0.25 $1.00\nSum $4.00"}
    p = scripted.propose_rows_parser(rec, rows_spec())
    assert p["fields"]["ref"] == {"regex": r"(?i)ref\s*[:#]?\s*(\S+)"}  # the label is the field's name, case-insensitive
    assert learn_value(p, rec, "ref") == "R-9"


def learn_value(parser: dict, rec: dict, name: str):
    from agents import learn
    return learn.extract_field(parser["fields"][name], rec, name)


def test_scripted_proposes_lines_for_text_without_tables(scripted):
    rec = pdf_rec()
    p = scripted.propose_rows_parser(rec, rows_spec())
    sel = p["rows"]["select"]["lines"]
    assert sel["after"] == r"^Item\b" and sel["before"] == r"^Sum\b"  # first words of the lines around the rows
    assert "(?P<item>" in sel["match"] and "(?P<each>" in sel["match"] and "(?P<line>" in sel["match"]
    assert p["rows"]["fields"]["line"] == {"column": "line", "clean": "money"}
    assert p["rows"]["fields"]["qty"] == {"column": "qty", "cast": "int"}
    assert [r.fields["line"] for r in extract_rows(rec, p["rows"], "w")] == [3.0, 1.0]
    assert "4.0" not in sel["match"] and "Bolt" not in sel["after"] + sel["before"]  # anchored on words, not values


def test_scripted_gives_up_when_a_declared_check_fails_or_nothing_looks_like_rows(scripted):
    assert scripted.propose_rows_parser(pdf_rec(PDF_TEXT.replace("Sum 4.00", "Sum 5.00")), rows_spec()) is None
    assert scripted.propose_rows_parser(pdf_rec("Ref R-9\nthanks for your order\nSum 4.00"), rows_spec()) is None
    no_columns = {**doc(), "tables": [{"header": ["A", "B"], "rows": [["1", "2"]]}], "text": "Ref R-9\nSum $4.00"}
    assert scripted.propose_rows_parser(no_columns, rows_spec()) is None  # no column for the row fields, no lines


def test_propose_parser_dispatches_documents_and_leaves_pages_alone(scripted):
    assert scripted.propose_parser(doc(), rows_spec())["rows"]["select"]["table"]
    page = {"id": "pg-000001", "site": "x.example", "html": "<h1>Hi</h1>", "text": "Hi", "title": "Hi"}
    spec = {"source": {"html_field": "html", "text_field": "text"}, "validate": [],
            "extract": {"parsers": [{"id": "p", "fields": {"title": {"css": "h1"}}}]}}
    assert "rows" not in (scripted.propose_parser(page, spec) or {})


def test_scripted_learn_on_a_document_auto_deploys_and_emits_the_deep_event_types(workspace, monkeypatch):
    set_learn_policy("auto")
    eng = FakeEngine().install(monkeypatch)
    eng.add_page(doc())
    eng.deploy_baseline(rows_spec(), [])
    svc = ScriptedAgentService(pace_s=0)
    tid = svc.start_learn("doc-000009", NAME)
    wait_for(svc, tid, "done")
    evs = check_events(svc, tid)
    types = [e["type"] for e in evs]
    # the deep agent's test_page_parser and save_parser each emit evidence: the same sequence on a document
    assert [t for t in types if t in ("evidence", "learned", "deployed")] == ["evidence", "evidence", "learned", "deployed"]
    ev = next(e for e in evs if e["type"] == "evidence")
    assert ev["rows"]["count"] == 2 and all(c["ok"] for c in ev["rows"]["checks"])
    learned = next(e for e in evs if e["type"] == "learned")
    assert learned["approval"] == "auto" and learned["deployed"] is True and learned["site"] == "south.example"
    done = next(e for e in evs if e["type"] == "done")["result"]
    assert done["model_calls"] == 0 and Store().active_version(NAME) == 2
    assert any(p["id"] == "south" and p["rows"] for p in active_spec()["extract"]["parsers"])


def test_scripted_learn_that_finds_nothing_leaves_the_document_held(workspace, monkeypatch):
    set_learn_policy("auto")
    eng = FakeEngine().install(monkeypatch)
    eng.add_page({**doc(), "tables": [], "text": "nothing to see"})
    eng.deploy_baseline(rows_spec(), [])
    svc = ScriptedAgentService(pace_s=0)
    tid = svc.start_learn("doc-000009", NAME)
    wait_for(svc, tid, "failed")
    assert "no checked reader" in next(e for e in svc.events(tid) if e["type"] == "error")["message"]
    assert Store().active_version(NAME) == 1
