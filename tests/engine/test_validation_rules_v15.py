"""V15 item 5: validation rules `number` (min/max), `date`, `url` in the spec, the runtime checks, the
compiler's validate() and its generated negative tests, on a page integration that is not about jobs."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from compiler.compile import Example, _negative_cases, compile_spec
from fixtures.products.pages import SPEC, body, examples, page_record
from runner.lib import Quarantine, check_date, check_number, check_url
from runner.loader import load_integration
from runner.pipeline import MemoryStore, process_record
from runner.testing import FakeHttp
from spec import load_spec, validate_spec

REPO = Path(__file__).resolve().parents[2]


def _spec_text(rules: str) -> str:
    text = SPEC.read_text()
    start = text.index("validate:")
    end = text.index("dedupe:")
    return text[:start] + rules + text[end:]


def test_spec_accepts_the_new_rules_and_checks_their_arguments():
    assert validate_spec(SPEC.read_text()) == []
    bad_min = _spec_text("validate:\n  - {field: name, rule: required, min: 1}\n")
    assert any("min/max only apply to rule 'number'" in e for e in validate_spec(bad_min))
    crossed = _spec_text("validate:\n  - {field: price, rule: number, min: 10, max: 1}\n")
    assert any("greater than max" in e for e in validate_spec(crossed))
    unknown = _spec_text("validate:\n  - {field: price, rule: money}\n")
    assert validate_spec(unknown)


@pytest.mark.parametrize("value,ok", [("24.99", True), (24.99, True), ("1,299", True), (0, True), ("100000", True),
                                      ("-1", False), ("100001", False), ("n/a", False), ("", False), (None, False),
                                      (True, False), ("nan", False)])
def test_number_rule(value, ok):
    fields = {"price": value}
    if ok:
        check_number(fields, "price", min=0, max=100000)
    else:
        with pytest.raises(Quarantine) as e:
            check_number(fields, "price", min=0, max=100000)
        assert e.value.code == "VALIDATION_FAILED" and e.value.detail.startswith("price: ")


@pytest.mark.parametrize("value,ok", [("2026-03-01", True), (" 2026-03-01 ", True), ("2026-02-30", False),
                                      ("14/11/2026", False), ("2026-3-1", False), ("", False), (None, False)])
def test_date_rule(value, ok):
    if ok:
        check_date({"released": value}, "released")
    else:
        with pytest.raises(Quarantine) as e:
            check_date({"released": value}, "released")
        assert e.value.code == "VALIDATION_FAILED" and "released" in e.value.detail


def test_date_rule_accepts_date_objects():
    import datetime
    check_date({"d": datetime.date(2026, 1, 2)}, "d")
    check_date({"d": datetime.datetime(2026, 1, 2, 10, 0)}, "d")


@pytest.mark.parametrize("value,ok", [("https://shop.example/p/1", True), ("http://x.org", True),
                                      ("/relative/path", False), ("ftp://x.org/f", False), ("https://", False),
                                      ("https://x.org/a b", False), ("", False), (None, False)])
def test_url_rule(value, ok):
    if ok:
        check_url({"image": value}, "image")
    else:
        with pytest.raises(Quarantine) as e:
            check_url({"image": value}, "image")
        assert e.value.code == "VALIDATION_FAILED" and "image" in e.value.detail


def test_negative_cases_cover_every_new_rule():
    ids = {c["id"] for c in _negative_cases(load_spec(SPEC))}
    assert {"price_not_a_number", "price_below_min", "price_above_max", "released_not_iso", "released_not_a_day",
            "image_relative", "image_not_http", "name_missing"} <= ids


@pytest.fixture(scope="module")
def product_pkg(tmp_path_factory):
    out = tmp_path_factory.mktemp("integrations")
    exs = [Example.parse(e) for e in examples()]
    result = compile_spec(load_spec(SPEC), version=1, out_root=out, examples=exs)
    return result, load_integration(result.path)


def test_compiled_validate_runs_the_new_rules(product_pkg):
    _, integ = product_pkg
    rec = page_record("shop-a.html", 1)
    out = process_record(integ, rec, FakeHttp(), MemoryStore())
    assert out.status == "success", out.detail
    assert out.body == body("shop-a.html", rec)
    _, fields = integ.extract(rec)
    for field, value in (("price", "-3"), ("released", "March 1"), ("image", "img/kettle.jpg")):
        with pytest.raises(Quarantine) as e:
            integ.validate({**fields, field: value}, rec, FakeHttp())
        assert e.value.code == "VALIDATION_FAILED" and e.value.detail.startswith(f"{field}: ")


def test_generated_suite_passes_including_negative_tests(product_pkg):
    result, _ = product_pkg
    proc = subprocess.run([sys.executable, "-m", "pytest", str(result.path / "tests"), "-q", "-p", "no:cacheprovider",
                           "-rA", "--rootdir", str(result.path), "-c", os.devnull],
                          capture_output=True, text=True, cwd=REPO, env={**os.environ, "PYTHONPATH": str(REPO)},
                          timeout=300)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
    for case in ("price_below_min", "price_above_max", "released_not_iso", "image_relative"):
        assert f"test_validation_rule_quarantines[{case}]" in proc.stdout and "PASSED" in proc.stdout
