import copy

import pytest
import yaml

from spec import SpecError, load_spec, parse_spec, validate_spec
from tests.conftest import FIXTURE_SPEC


@pytest.fixture
def raw():
    return yaml.safe_load(FIXTURE_SPEC.read_text())


def test_fixture_spec_is_valid():
    spec = load_spec(FIXTURE_SPEC)
    assert spec.name == "po_to_stockroom"
    assert [p.id for p in spec.extract.parsers] == ["acme_v1", "globex_v1", "initech_v1"]


def errors_for(raw) -> list[str]:
    return validate_spec(yaml.safe_dump(raw))


def test_unknown_transform_is_rejected_with_the_closed_set(raw):
    raw["sink"]["body"]["quantity"] = {"transform": "sqrt", "from": "qty"}
    errs = errors_for(raw)
    assert any("unknown transform 'sqrt'" in e and "lookup" in e for e in errs), errs


def test_field_missing_from_one_parser_is_rejected(raw):
    del raw["extract"]["parsers"][1]["fields"]["sku"]
    errs = errors_for(raw)
    assert any("'sku'" in e and "not every parser" in e for e in errs), errs


def test_regex_needs_a_capture_group(raw):
    raw["extract"]["parsers"][0]["fields"]["qty"] = {"regex": r"Qty:\s*\d+", "cast": "int"}
    assert any("capture group" in e for e in errors_for(raw))


def test_bad_regex_is_reported(raw):
    raw["extract"]["parsers"][0]["fields"]["qty"] = {"regex": r"Qty:(\d+", "cast": "int"}
    assert any("does not compile" in e for e in errors_for(raw))


def test_extra_keys_are_rejected(raw):
    raw["sink"]["retry"] = 3
    assert any("retry" in e for e in errors_for(raw))


def test_value_source_needs_exactly_one_shape(raw):
    raw["sink"]["body"]["quantity"] = {"from": "qty", "const": 5}
    assert errors_for(raw)


def test_const_and_record_refs_and_templates_are_allowed(raw):
    raw["sink"]["body"]["note"] = {"template": "from {record.sender} re {po_id}"}
    raw["sink"]["body"]["channel"] = {"const": "email"}
    raw["sink"]["body"]["warehouse_id"] = {"transform": "lookup", "from": "record.sender_domain",
                                           "args": {"table": {"acme.com": "BOS"}}}
    assert errors_for(raw) == []


def test_validate_rules_cannot_target_record_fields(raw):
    raw["validate"].append({"field": "record.sender", "rule": "required"})
    assert any("record.*" in e for e in errors_for(raw))


def test_spec_error_lists_every_problem(raw):
    bad = copy.deepcopy(raw)
    bad["name"] = "Bad Name"
    bad["dedupe"]["key"] = ["nope"]
    with pytest.raises(SpecError) as exc:
        parse_spec(bad)
    assert len(exc.value.errors) >= 1
    assert "Bad Name" in str(exc.value)


def test_validate_spec_never_raises_on_garbage():
    assert validate_spec(": : :")
    assert validate_spec("- just a list")


UMBRELLA_QTY = r"Item code: \S+ / (\d+) cases \((\d+)/case\)"


def test_combine_product_needs_two_groups_and_a_numeric_cast(raw):
    fields = raw["extract"]["parsers"][0]["fields"]
    fields["qty"] = {"regex": UMBRELLA_QTY, "cast": "int", "combine": "product"}
    assert errors_for(raw) == []
    fields["qty"] = {"regex": r"Qty:\s*(\d+)", "cast": "int", "combine": "product"}
    assert any("at least two capture groups" in e for e in errors_for(raw))
    fields["qty"] = {"regex": UMBRELLA_QTY, "combine": "product"}
    assert any("cast: int or float" in e for e in errors_for(raw))
    fields["qty"] = {"between": ["a", "b"], "cast": "int", "combine": "product"}
    assert any("combine only applies to regex" in e for e in errors_for(raw))
    fields["qty"] = {"regex": UMBRELLA_QTY, "cast": "int", "combine": "sum"}
    assert errors_for(raw), "combine is a closed set"
