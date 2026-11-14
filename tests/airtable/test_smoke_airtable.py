"""`make smoke-v2-airtable-offline`: the whole 2-minute script (scripts/smoke_v2.py) with an Airtable destination,
on an in-process stack (fake Airtable without an Email field like the real base, control plane, scripted agent)."""

from __future__ import annotations

import importlib.util

import pytest

from bench.v2.client import presenter_answer
from bench.v2.stack import missing
from tests.airtable.helpers import REPO


def _smoke():
    spec = importlib.util.spec_from_file_location("smoke_v2", REPO / "scripts" / "smoke_v2.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_presenter_skips_a_question_the_table_has_no_field_for():
    q = "The form question 'Email' has no field with that name in the Airtable table 'Applications'. Which field should get it?"
    assert presenter_answer(q, ["Skip this field"])[0] == "Skip this field"
    assert presenter_answer(q, ["Contact", "Skip this field"])[0] == "Skip this field"


def test_smoke_v2_airtable_in_process(tmp_path, capsys):
    gaps = missing()
    if gaps:
        pytest.skip("needs the integrated stack: " + "; ".join(gaps))
    assert _smoke().main(["--sink", "airtable", "--in-process", "--workspace", str(tmp_path), "--timeout", "120"]) == 0
    out = capsys.readouterr().out
    assert "-> 'Skip this field'" in out and "9 rows confirmed: 6 rows" in out


def test_real_base_mode_needs_a_base_id(monkeypatch, capsys):
    monkeypatch.delenv("PATCHWORK_AIRTABLE_BASE_ID", raising=False)
    smoke = _smoke()
    monkeypatch.setattr(smoke, "Control", lambda base: type("C", (), {"get": lambda self, p: {}})())
    assert smoke.main(["--sink", "airtable"]) == 2
    assert "PATCHWORK_AIRTABLE_BASE_ID" in capsys.readouterr().err
