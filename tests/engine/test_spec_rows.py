"""G1: spec additions for one document -> many records (contracts V18): `rows`, `clean`, cross-check rules."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from spec import spec_hash, spec_to_dict
from spec.models import FieldExtract, RowExtract, Spec

REPO = Path(__file__).resolve().parents[2]

# Example 1 of docs/superpowers/specs/2026-11-14-generalized-input-design.md, verbatim.
DESIGN_EXAMPLE_1 = r"""
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
"""


def yload(text: str) -> dict:
    return yaml.safe_load(text)


def with_second_parser_without_rows(text: str) -> dict:
    data = yload(text)
    second = copy.deepcopy(data["extract"]["parsers"][0])
    second["id"], second["match"] = "contoso", {"site": "contoso.example"}
    del second["rows"]
    data["extract"]["parsers"].append(second)
    return data


def replace_rule(text: str, rule: dict, index: int = 1) -> dict:
    data = yload(text)
    data["validate"][index] = rule
    return data


def test_invoice_example_from_design_parses():
    spec = Spec.model_validate(yload(DESIGN_EXAMPLE_1))
    p = spec.extract.parsers[0]
    assert p.rows.select.table.columns == ["Description", "Qty", "Unit Price", "Amount"]
    assert p.all_fields() == {"invoice_no", "invoice_date", "subtotal", "description", "qty", "unit_price", "amount"}
    assert p.rows.require == ["description"] and p.rows.hold == "document"
    assert p.fields["invoice_no"].refine == r"Invoice\s+(\S+)"  # a regex next to json_path is a refine
    assert p.fields["subtotal"].clean == "money"
    rules = {r.rule: r for r in spec.validate_}
    assert rules["sum_equals"].rows_field == "amount" and rules["sum_equals"].tolerance == 0.01
    assert rules["product_equals"].fields == ["qty", "unit_price"]


def test_mixed_rows_and_no_rows_parsers_rejected():
    with pytest.raises(ValidationError, match="every parser has rows or none"):
        Spec.model_validate(with_second_parser_without_rows(DESIGN_EXAMPLE_1))


def test_cross_check_needs_rows_fields():
    bad = replace_rule(DESIGN_EXAMPLE_1, {"rule": "sum_equals", "rows_field": "subtotal", "field": "subtotal"})
    with pytest.raises(ValidationError, match="rows_field 'subtotal' is not a row field"):
        Spec.model_validate(bad)


def test_cross_check_shapes():
    bad = replace_rule(DESIGN_EXAMPLE_1, {"rule": "product_equals", "fields": ["qty"], "field": "amount"}, 2)
    with pytest.raises(ValidationError, match="two fields"):
        Spec.model_validate(bad)
    bad = replace_rule(DESIGN_EXAMPLE_1, {"rule": "product_equals", "fields": ["qty", "subtotal"], "field": "amount"}, 2)
    with pytest.raises(ValidationError, match="'subtotal' is not a row field"):
        Spec.model_validate(bad)
    bad = replace_rule(DESIGN_EXAMPLE_1, {"rule": "sum_equals", "field": "subtotal"})
    with pytest.raises(ValidationError, match="needs 'rows_field'"):
        Spec.model_validate(bad)
    bad = replace_rule(DESIGN_EXAMPLE_1, {"rule": "sum_equals", "rows_field": "amount", "field": "qty"})
    with pytest.raises(ValidationError, match="'qty' is not a document field"):
        Spec.model_validate(bad)
    bad = replace_rule(DESIGN_EXAMPLE_1, {"rule": "required", "field": "invoice_no", "tolerance": 0.5}, 0)
    with pytest.raises(ValidationError, match="tolerance"):
        Spec.model_validate(bad)
    ok = replace_rule(DESIGN_EXAMPLE_1, {"rule": "count_equals", "field": "subtotal"})
    assert Spec.model_validate(ok).validate_[1].tolerance == 0.01


def test_cross_check_rules_need_rows():
    data = yload((REPO / "fixtures" / "spec.yaml").read_text())
    field = next(iter(data["extract"]["parsers"][0]["fields"]))
    data["validate"].append({"rule": "count_equals", "field": field})
    with pytest.raises(ValidationError, match="only apply when every parser has rows"):
        Spec.model_validate(data)


def test_row_and_document_field_names_must_not_collide():
    data = yload(DESIGN_EXAMPLE_1)
    data["extract"]["parsers"][0]["rows"]["fields"]["subtotal"] = {"column": "Amount"}
    with pytest.raises(ValidationError, match="'subtotal' is both a document field and a row field"):
        Spec.model_validate(data)


def test_rows_shape_errors():
    data = yload(DESIGN_EXAMPLE_1)
    rows = data["extract"]["parsers"][0]["rows"]
    for select, msg in [({}, "exactly one of table/css/lines"),
                        ({"table": {"columns": []}}, "columns must not be empty"),
                        ({"css": "tr", "table": {"columns": ["A"]}}, "exactly one of table/css/lines"),
                        ({"lines": {"match": "(unclosed"}}, "does not compile")]:
        bad = copy.deepcopy(data)
        bad["extract"]["parsers"][0]["rows"]["select"] = select
        with pytest.raises(ValidationError, match=msg):
            Spec.model_validate(bad)
    bad = copy.deepcopy(data)
    bad["extract"]["parsers"][0]["rows"]["require"] = ["nope"]
    with pytest.raises(ValidationError, match="require names 'nope'"):
        Spec.model_validate(bad)
    bad = copy.deepcopy(data)
    bad["extract"]["parsers"][0]["rows"]["fields"]["description"] = {"css": "td"}
    with pytest.raises(ValidationError, match="css row fields need select.css"):
        Spec.model_validate(bad)
    assert rows["select"]["table"]["columns"]


def test_row_extract_shape():
    assert RowExtract(column="Qty", regex=r"(\d+)").refine == r"(\d+)"
    assert RowExtract(regex=r"(\d+)").refine is None
    for bad in ({}, {"column": "A", "index": 1}, {"index": -1}, {"attr": "href", "column": "A"},
                {"regex": "no group"}, {"column": " "}):
        with pytest.raises(ValidationError):
            RowExtract(**bad)


def test_lines_select_parses():
    data = yload(DESIGN_EXAMPLE_1)
    data["source"].pop("html_field")
    data["extract"]["parsers"][0]["rows"] = {
        "select": {"lines": {"after": r"^Description\s+Qty", "before": "^Subtotal",
                             "match": r"^(?P<description>.+?)\s+(?P<qty>\d+)\s+(?P<unit>\S+)\s+(?P<amount>\S+)$"}},
        "fields": {"description": {"column": "description"}, "qty": {"column": "qty", "cast": "int"},
                   "unit_price": {"column": "unit", "clean": "money"}, "amount": {"index": 3, "clean": "money"}}}
    p = Spec.model_validate(data).extract.parsers[0]
    assert p.rows.select.lines.before == "^Subtotal" and p.rows.select.lines.skip is None


def test_css_rows_need_html_field():
    data = yload(DESIGN_EXAMPLE_1)
    data["source"].pop("html_field")
    data["extract"]["parsers"][0]["rows"] = {"select": {"css": "div.item"}, "fields": {"description": {"css": "b"}}}
    with pytest.raises(ValidationError, match="source.html_field"):
        Spec.model_validate(data)


def test_unknown_cleaner_and_dateless_date_rejected():
    for clean in ("guess", {"date": []}, "date", {"split": {"part": 1}}, {"money": {"decimal": ";"}},
                  {"money": {}, "date": ["%Y"]}):
        with pytest.raises(ValidationError):
            FieldExtract(regex="(x)", clean=clean)
    for clean in ("money", "number", "strip", "upper", "lower", {"money": {"decimal": ","}},
                  {"date": ["%d %b %Y"]}, {"split": {"sep": ",", "part": 1}}):
        assert FieldExtract(regex="(x)", clean=clean).clean == clean


def test_json_path_refine_and_combine_rules():
    f = FieldExtract(json_path="$.meta.subject", regex=r"Invoice\s+(\S+)")
    assert f.refine == r"Invoice\s+(\S+)" and not f.reads_html
    assert FieldExtract(regex="(x)").refine is None
    with pytest.raises(ValidationError):
        FieldExtract(json_path="$.a", regex=r"(\d+) x (\d+)", combine="product", cast="int")
    with pytest.raises(ValidationError):
        FieldExtract(json_path="$.a", regex="no group")


def test_record_row_refs_are_valid():
    spec = Spec.model_validate(yload(DESIGN_EXAMPLE_1))
    assert spec.dedupe.key == ["invoice_no", "record.row_no"]


def test_existing_specs_still_validate():
    for p in ("fixtures/spec.yaml", "fixtures/jobs/spec.yaml", "fixtures/products/spec.yaml"):
        Spec.model_validate(yload((REPO / p).read_text()))


HASHES_BEFORE_V18 = {
    "fixtures/spec.yaml": "88d4fced8f023b4a860b28c2f450c7a65ff8314e60ae352d6a19b688ff893454",
    "fixtures/jobs/spec.yaml": "e68a4112e462fd1fa427edb2ab8073d4cf10ae2a33057f574d83d07081a9c9cc",
    "fixtures/products/spec.yaml": "9724c4f8dd403f540dc9e2147c0adac026a98b577d60111718f271cc1b54f105",
}


@pytest.mark.parametrize("path", sorted(HASHES_BEFORE_V18))
def test_existing_specs_dump_and_hash_as_before(path):
    """A spec without rows/clean/cross-checks dumps (and so hashes) exactly as before V18."""
    spec = Spec.model_validate(yload((REPO / path).read_text()))
    dumped = yaml.safe_dump(spec_to_dict(spec))
    for key in ("rows", "clean", "tolerance", "rows_field"):
        assert f"{key}:" not in dumped, key
    assert spec_hash(spec) == HASHES_BEFORE_V18[path]
