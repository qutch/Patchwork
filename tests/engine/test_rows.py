"""G2: the rows engine (contracts V18): table, css and lines selectors; explained skips; cross-checks.

Tables normally come from the teammate's runner.documents.read_html (a page record) or from record["tables"]
(a V17 document record). Until read_html lands, the tests that need it are skipped and the same cases run on
record["tables"], built here by a test-only reader."""

from __future__ import annotations

from pathlib import Path

import pytest
from selectolax.lexbor import LexborHTMLParser

from runner.html import visible_text
from runner.lib import Quarantine
from runner.rows import Row, check_cross, explain_skip, extract_rows, select_rows

TABLE = ("<table><tr><th>Item</th><th>Qty</th><th>Each</th><th>Line</th></tr>"
         "<tr><td>Bolt</td><td>2</td><td>$1.50</td><td>$3.00</td></tr>"
         "<tr><td>Nut</td><td>4</td><td>$0.25</td><td>$1.00</td></tr>"
         "<tr><td></td><td></td><td>Sum</td><td>$4.00</td></tr></table>")
ROWS = {"select": {"table": {"columns": ["Item", "Qty"]}},
        "fields": {"item": {"column": "Item"}, "qty": {"column": "Qty", "cast": "int"}, "line": {"column": "Line", "clean": "money"}}}
UNEXPLAINED = TABLE.replace("<td></td><td></td><td>Sum</td><td>$4.00</td>",
                            "<td></td><td>9</td><td>$1.00</td><td>$9.00</td>")


def tables_of(html: str) -> list[dict]:
    """Test-only stand-in for runner.documents.read_html(...).tables, as V17 dicts."""
    out = []
    for i, t in enumerate(LexborHTMLParser(html).css("table"), 1):
        trs = [[" ".join(c.text().split()) for c in tr.css("th,td")] for tr in t.css("tr")]
        out.append({"header": trs[0], "rows": trs[1:], "where": f"table {i}"})
    return out


# ---- the brief's cases on HTML (need the teammate's read_html) ---------------------------------------------

def test_html_table_rows_skip_and_explain_footer():
    pytest.importorskip("runner.documents")
    rows = extract_rows({"id": "d1", "html": TABLE, "text": visible_text(TABLE)}, ROWS, "p.rows")
    assert [(r.no, r.fields) for r in rows] == [(1, {"item": "Bolt", "qty": 2, "line": 3.0}), (2, {"item": "Nut", "qty": 4, "line": 1.0})]


def test_unexplained_skipped_row_holds_the_document():
    pytest.importorskip("runner.documents")
    with pytest.raises(Quarantine, match="row 3 skipped but not blank"):
        extract_rows({"id": "d1", "html": UNEXPLAINED, "text": ""}, ROWS, "p.rows")


# ---- the same cases on record["tables"] (V17 document records) ---------------------------------------------

def test_tables_rows_skip_and_explain_footer():
    skipped: list[dict] = []
    rows = extract_rows({"id": "d1", "html": "", "text": visible_text(TABLE), "tables": tables_of(TABLE)}, ROWS,
                        "p.rows", skipped=skipped)
    assert [(r.no, r.fields) for r in rows] == [(1, {"item": "Bolt", "qty": 2, "line": 3.0}),
                                                (2, {"item": "Nut", "qty": 4, "line": 1.0})]
    assert rows[0].cells == {"Item": "Bolt", "Qty": "2", "Each": "$1.50", "Line": "$3.00"}
    assert rows[0].text == "Bolt 2 $1.50 $3.00"
    assert skipped == [{"row": 3, "text": "Sum $4.00", "reason": "aggregate"}]


def test_tables_unexplained_skipped_row_holds_the_document():
    with pytest.raises(Quarantine, match="row 3 skipped but not blank, a header or an aggregate: '9 \\$1.00 \\$9.00'") as exc:
        extract_rows({"id": "d1", "tables": tables_of(UNEXPLAINED)}, ROWS, "p.rows")
    assert exc.value.code == "PARSE_NO_MATCH" and exc.value.detail.startswith("p.rows: ")


def test_tables_repeated_header_and_blank_rows_are_explained():
    t = {"header": ["Item", "Qty"], "rows": [["Bolt", "2"], ["", ""], ["Item", "Qty"], ["Nut", "4"]], "where": "t"}
    skipped: list[dict] = []
    rows = extract_rows({"tables": [t]}, {"select": {"table": {"columns": ["qty", " ITEM "]}},
                                          "fields": {"item": {"column": "item"}, "qty": {"index": 1, "cast": "int"}}},
                        "w", skipped=skipped)
    assert [(r.no, r.fields["item"], r.fields["qty"]) for r in rows] == [(1, "Bolt", 2), (2, "Nut", 4)]
    assert [s["reason"] for s in skipped] == ["blank", "header"]


def test_tables_first_matching_table_and_errors():
    other = {"header": ["A", "B"], "rows": [["1", "2"]], "where": "t1"}
    t = {"header": ["Item", "Qty"], "rows": [["Bolt", "2"]], "where": "t2"}
    rows = extract_rows({"tables": [other, t]}, ROWS | {"fields": {"item": {"column": "Item"}}}, "w")
    assert [r.fields for r in rows] == [{"item": "Bolt"}]
    with pytest.raises(Quarantine, match="no table has the columns") as exc:
        extract_rows({"tables": [other]}, ROWS, "w")
    assert exc.value.code == "PARSE_NO_MATCH"
    with pytest.raises(Quarantine, match="column 'Line' is not in the table header"):
        extract_rows({"tables": [t]}, ROWS, "w")  # ROWS reads Line, which this table lacks
    with pytest.raises(Quarantine, match="no rows"):
        extract_rows({"tables": [{"header": ["Item", "Qty"], "rows": [["", ""]], "where": "t"}]},
                     {"select": ROWS["select"], "fields": {"item": {"column": "Item"}}}, "w")


def test_bad_cell_value_holds_the_document_and_names_the_row():
    t = {"header": ["Item", "Qty"], "rows": [["Bolt", "2"], ["Nut", "four"]], "where": "t"}
    with pytest.raises(Quarantine, match="row 2") as exc:
        extract_rows({"tables": [t]}, {"select": ROWS["select"], "fields": {"item": {"column": "Item"},
                                                                          "qty": {"column": "Qty", "cast": "int"}}}, "w")
    assert exc.value.code == "PARSE_NO_MATCH"


def test_empty_optional_cell_is_none_and_refine_applies():
    t = {"header": ["Item", "Note"], "rows": [["Bolt", ""], ["Nut", "size M8"]], "where": "t"}
    rows = extract_rows({"tables": [t]}, {"select": {"table": {"columns": ["Item"]}},
                                          "fields": {"item": {"column": "Item"},
                                                     "size": {"column": "Note", "regex": r"size (\S+)"}}}, "w")
    assert [r.fields["size"] for r in rows] == [None, "M8"]


def test_select_rows_returns_text_and_cells():
    got = select_rows({"tables": tables_of(TABLE)}, ROWS["select"])
    assert got[0] == ("Bolt 2 $1.50 $3.00", {"Item": "Bolt", "Qty": "2", "Each": "$1.50", "Line": "$3.00"})
    assert len(got) == 3


# ---- css and lines -----------------------------------------------------------------------------------------

def test_css_repeated_blocks_and_lines_select():
    html = '<div class="it"><b>Lamp</b><i>x2</i></div><div class="it"><b>Desk</b><i>x1</i></div>'
    rows = extract_rows({"html": html, "text": ""}, {"select": {"css": "div.it"},
                        "fields": {"name": {"css": "b"}, "n": {"css": "i", "regex": r"x(\d+)", "cast": "int"}}}, "w")
    assert [r.fields for r in rows] == [{"name": "Lamp", "n": 2}, {"name": "Desk", "n": 1}]
    text = "Items\nLamp  2\nDesk  1\nThanks"
    rows = extract_rows({"text": text}, {"select": {"lines": {"after": "^Items$", "before": "^Thanks$",
                        "match": r"^(?P<name>.+?)\s{2,}(?P<n>\d+)$"}}, "fields": {"name": {"regex": "(.+)"}, "n": {"regex": r"(\d+)", "cast": "int"}}}, "w")
    assert [r.fields["n"] for r in rows] == [2, 1]


def test_css_attr_and_html_field():
    html = '<ul><li><a href="/a">A</a></li><li><a href="/b">B</a></li><li></li></ul>'
    rows = extract_rows({"page": html}, {"select": {"css": "li"}, "fields": {"link": {"css": "a", "attr": "href"}}},
                        "w", html_field="page")
    assert [r.fields["link"] for r in rows] == ["/a", "/b"]  # the empty <li> is a blank row, explained


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


def test_lines_columns_are_named_groups_case_insensitive_and_index():
    text = "Header\nItem Qty\nLamp 2 $3.00\n\nDesk 1 $1.50\nTotal 3"
    rows = extract_rows({"body": text}, {"select": {"lines": {"after": "^Item", "before": "^Total",
                                                              "match": r"^(?P<Name>\S+) (?P<n>\d+) (?P<amt>\S+)$"}},
                                         "fields": {"name": {"column": "name"}, "amt": {"index": 2, "clean": "money"}}},
                        "w", text_field="body")
    assert [(r.no, r.fields, r.cells) for r in rows] == [
        (1, {"name": "Lamp", "amt": 3.0}, {"Name": "Lamp", "n": "2", "amt": "$3.00"}),
        (2, {"name": "Desk", "amt": 1.5}, {"Name": "Desk", "n": "1", "amt": "$1.50"})]


def test_lines_missing_markers_hold():
    with pytest.raises(Quarantine, match="after"):
        extract_rows({"text": "Lamp 2\nTotal 2"}, LINES, "w")
    with pytest.raises(Quarantine, match="before"):
        extract_rows({"text": "Item Qty\nLamp 2"}, LINES, "w")
    with pytest.raises(Quarantine, match="no rows"):
        extract_rows({"text": "Item Qty\nTotal 0"}, LINES, "w")


# ---- explain_skip and cross-checks ---------------------------------------------------------------------------

def test_explain_skip():
    header = ["Item", "Qty", "Line"]
    kept = [["Bolt", "2", "$3.00"], ["Nut", "4", "$1.00"]]
    assert explain_skip("", ["", "", ""], header, kept) == "blank"
    assert explain_skip("Item Qty Line", ["item", "QTY", "Line"], header, kept) == "header"
    assert explain_skip("6 $4.00", ["", "6", "$4.00"], header, kept) == "aggregate"
    assert explain_skip("Total $4.00", ["Total", "", "$4.00"], header, kept) == "aggregate"
    assert explain_skip("9 $9.00", ["", "9", "$9.00"], header, kept) is None
    assert explain_skip("note", ["", "", "see below"], header, kept) is None
    assert explain_skip("$4.00", ["", "", "$4.00"], header, []) is None


def test_cross_checks():
    rows = [Row(1, "", {}, {"q": 2, "p": 1.5, "a": 3.0}), Row(2, "", {}, {"q": 4, "p": 0.25, "a": 1.0})]
    assert check_cross({"rule": "sum_equals", "rows_field": "a", "field": "s", "tolerance": 0.01}, {"s": 4.0}, rows) is None
    assert "3,205.00" in check_cross({"rule": "sum_equals", "rows_field": "a", "field": "s", "tolerance": 0.01}, {"s": 3205}, rows)
    assert check_cross({"rule": "product_equals", "fields": ["q", "p"], "field": "a", "tolerance": 0.01}, {}, rows) is None
    assert check_cross({"rule": "count_equals", "field": "n"}, {"n": 3}, rows) == "count_equals: 2 rows but n is 3"


def test_cross_check_details_name_both_numbers():
    rows = [Row(1, "", {}, {"a": 3000.0}), Row(2, "", {}, {"a": 205.0})]
    detail = check_cross({"rule": "sum_equals", "rows_field": "a", "field": "s"}, {"s": 3215.0}, rows)
    assert detail == "sum_equals: the rows' a add up to 3,205.00 but s is 3,215.00"
    assert check_cross({"rule": "sum_equals", "rows_field": "a", "field": "s"}, {"s": "3,205.00"}, rows) is None
    bad = [Row(1, "", {}, {"q": 3, "p": 1.25, "a": 3.0})]
    assert check_cross({"rule": "product_equals", "fields": ["q", "p"], "field": "a"}, {}, bad) == \
        "product_equals: row 1: q × p = 3.75 but a is 3.00"
    assert "not a number" in check_cross({"rule": "sum_equals", "rows_field": "a", "field": "s"}, {"s": None}, rows)
    assert "row 1" in check_cross({"rule": "sum_equals", "rows_field": "x", "field": "s"}, {"s": 1}, rows)
    assert check_cross({"rule": "count_equals", "field": "n"}, {"n": "2"}, rows) is None


def test_no_subject_words_in_the_rows_engine():
    for name in ("runner/rows.py", "runner/clean.py"):
        text = (Path(__file__).resolve().parents[2] / name).read_text().lower()
        for word in ("invoice", "vendor", "order"):
            assert word not in text, (name, word)
