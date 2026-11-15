"""`make smoke-new-integration-offline` (plan A Task 9): one New integration wizard flow for three unrelated
domains, each on its own in-process stack (control plane, scripted agent, fake destination on a free port):
products -> fake Airtable, events -> fixture form, papers -> fake papers API. Every beat must print `ok`."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from bench.v2.stack import missing

REPO = Path(__file__).resolve().parents[2]
BEATS = ["1 new integration", "2 destination", "3 post 2 example pages", "4 teach from the examples", "5 approve",
         "6 rows confirmed", "7 a third site: learned by LEARN", "8 rename a destination field: held, repaired, written"]


def _smoke():
    spec = importlib.util.spec_from_file_location("smoke_v2", REPO / "scripts" / "smoke_v2.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("domain", ["products", "events", "papers"])
def test_new_integration_smoke(domain, tmp_path, capsys):
    gaps = missing()
    if gaps:
        pytest.skip("needs the integrated stack: " + "; ".join(gaps))
    rc = _smoke().main(["--new-integration", domain, "--in-process", "--workspace", str(tmp_path), "--timeout", "120"])
    captured = capsys.readouterr()
    assert rc == 0, captured.out + captured.err
    ok = re.findall(r"^\s+\d+\.\ds\s+ok\s+(.+)$", captured.out, flags=re.M)
    assert len(ok) == len(BEATS) and all(line.startswith(b) for line, b in zip(ok, BEATS)), captured.out
    assert "FAIL" not in captured.out
    assert f"new integration ({domain}) OK" in captured.out
    assert "learned_now, 0 AI calls" in ok[6]
    # D2: the papers site's values are only in citation_* meta (not corroborated), so a person approves its parser;
    # the product and event sites corroborate every value, so their parsers deploy automatically.
    assert ok[6].split(" approved_by ")[1].split(";")[0] == ("human" if domain == "papers" else "auto"), ok[6]
    assert "held (incident #" in ok[7] and "written under" in ok[7]


def test_new_integration_needs_in_process(capsys):
    assert _smoke().main(["--new-integration", "papers"]) == 2
    assert "--in-process" in capsys.readouterr().err


def test_teach_asks_and_the_person_picks_the_named_option():
    events = _smoke().DOMAINS["events"]
    q = "Nothing on the first example page clearly fills 'Start Date' (text). Where should it come from?"
    opts = ['page: meta[property="og:site_name"] (content)', "page: JSON-LD startDate (Event)", "Skip this field"]
    assert events.answer(q, opts)[0] == "page: JSON-LD startDate (Event)"
    other = "Nothing on the first example page clearly fills 'Store' (text). Where should it come from?"
    assert events.answer(other, opts)[0] == "Skip this field"
