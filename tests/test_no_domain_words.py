"""The input layer is domain-free (CLAUDE.md hard rule): no subject words (invoice, order, ticket, ...) in the code
that reads documents, the document prompt or the learn tool descriptions. Field names and meaning come only from the
spec. Fixtures, demos and tests may use subject words. Paths that do not exist yet are skipped, so each one is guarded
as it lands (the readers and connectors are the teammate's)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DOMAIN_WORDS = re.compile(
    r"\b(invoices?|vendors?|suppliers?|purchase|shipping|shipments?|carriers?|tickets?|attendees?|registrations?|"
    r"customers?|subtotal|tax|prices?)\b", re.I)

INPUT_LAYER = ["runner/documents.py", "runner/imap.py", "control/connectors", "control/documents.py",
               "control/inboxes.py", "control/api/documents.py", "runner/rows.py", "runner/clean.py"]


def python_files(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for rel in paths:
        p = REPO / rel
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            out += sorted(p.rglob("*.py"))
    return out


def test_the_guard_finds_the_files_that_exist():
    names = {f.name for f in python_files(INPUT_LAYER)}
    assert {"documents.py", "inboxes.py", "rows.py", "clean.py"} <= names  # the guard is not vacuous


@pytest.mark.parametrize("path", python_files(INPUT_LAYER), ids=lambda p: str(p.relative_to(REPO)))
def test_input_layer_has_no_domain_words(path):
    hits = sorted({m.group(0).lower() for m in DOMAIN_WORDS.finditer(path.read_text())})
    assert not hits, f"{path.relative_to(REPO)} names a subject: {hits}"


def test_document_prompt_and_learn_tool_descriptions_have_no_domain_words():
    from agents import deep_tools, prompts

    empty = {"intent": "", "extract": {"parsers": []}}
    assert not DOMAIN_WORDS.search(prompts.document_learner_prompt(empty))
    assert not DOMAIN_WORDS.search(prompts.DOCUMENT_LEARN_NOTE)
    assert not DOMAIN_WORDS.search(deep_tools._ROWS_HINT)
    for tool in ("get_page", "test_page_parser", "save_parser"):
        assert not DOMAIN_WORDS.search(deep_tools.TOOL_DOCS[tool]), tool


def test_the_prompt_names_only_what_the_spec_gives():
    from agents import prompts

    spec = {"name": "order_lines", "intent": "Copy each row of a table",
            "extract": {"parsers": [{"fields": {"ref": {}, "total": {}}, "rows": {"fields": {"item": {}, "qty": {}}}}]},
            "validate": [{"rule": "sum_equals", "field": "total", "rows_field": "qty"}]}
    text = prompts.document_learner_prompt(spec)
    assert "order_lines" in text and "`ref`, `total`" in text and "`item`, `qty`" in text and "sum_equals on total" in text
    assert "order_lines" not in prompts.document_learner_prompt(empty := {"intent": "", "extract": {"parsers": []}})
    assert empty["intent"] == ""
