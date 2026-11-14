"""Matcher (stage A): provenance, candidate regexes, normalisation, unexplained values."""

from __future__ import annotations

import ast
import copy
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from demo_apps.seed import generate
from matcher import char_class, label_regex, left_label, match

REPO = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((REPO / "fixtures" / "recording.json").read_text())
FIXTURE_SPEC = yaml.safe_load((REPO / "fixtures" / "spec.yaml").read_text())


@pytest.fixture
def candidates():
    return match(FIXTURE)


def _with(record: dict, sink_body: dict, base=FIXTURE) -> dict:
    """The fixture recording with a different source record and sink body."""
    rec = copy.deepcopy(base)
    for e in rec["events"]:
        if e["type"] == "net" and e["method"] == "GET":
            e["resp"] = [record] if isinstance(e["resp"], list) else record
        elif e["type"] == "net" and e["method"] == "POST":
            e["req"] = sink_body
    return rec


def test_fixture_candidates_follow_the_contract(candidates):
    assert candidates["recording_id"] == "fixture-po-001"
    assert candidates["intent"] == FIXTURE["intent"]
    assert {k: candidates["sink"][k] for k in ("base_url", "method", "path", "body")} == {
        "base_url": "http://localhost:8002", "method": "POST", "path": "/api/incoming",
        "body": {"product_id": "GAD-217", "quantity": 20, "reference": "PO-1022"}}
    src = candidates["source"]
    assert src["base_url"] == "http://localhost:8001"
    assert src["list"] == {"method": "GET", "path": "/api/messages"}
    assert (src["record_id"], src["text_field"], src["record"]["id"]) == ("id", "body", "msg-0007")
    assert candidates["unexplained"] == []

    m = {x["dest_field"]: x for x in candidates["mappings"]}
    assert {k: m["quantity"][k] for k in ("value", "kind", "source_field", "label", "candidate_regex", "cast")} == {
        "value": 20, "kind": "text", "source_field": "body", "label": "Qty:", "candidate_regex": r"Qty:\s*(\d+)",
        "cast": "int"}
    body = src["record"]["body"]
    for f, value in (("quantity", "20"), ("product_id", "GAD-217"), ("reference", "PO-1022")):
        start, end = m[f]["offset"]
        assert body[start:end] == value
        assert m[f]["regex_ok"]
    assert m["product_id"]["candidate_regex"] == r"SKU:\s*([A-Z]+-\d+)"
    assert m["reference"]["candidate_regex"] == r"PO\s+Number:\s*([A-Z]+-\d+)"
    assert m["reference"]["label"] == "PO Number:" and m["reference"]["copied"] is True
    # the subject also contains the PO; it is offered as an alternative, not chosen
    assert m["reference"]["alternatives"][0]["source_field"] == "subject"


def test_regexes_extract_every_acme_message_in_the_seed(candidates):
    """The generated regexes must pull the right values out of every acme.com message."""
    rx = {m["dest_field"]: re.compile(m["candidate_regex"]) for m in candidates["mappings"]}
    acme = next(p for p in FIXTURE_SPEC["extract"]["parsers"] if p["id"] == "acme_v1")["fields"]
    reference = {"reference": re.compile(acme["po_id"]["regex"]), "product_id": re.compile(acme["sku"]["regex"]),
                 "quantity": re.compile(acme["qty"]["regex"])}
    seed = generate()
    checked = 0
    for msg in seed.messages:
        if msg["sender_domain"] != "acme.com":
            continue
        gt = seed.ground_truth[msg["id"]]
        if gt["reason"] == "PARSE_NO_MATCH":  # junk (holiday notice): must not produce a value
            assert all(r.search(msg["body"]) is None for r in rx.values()), msg["body"]
            continue
        got = {f: r.search(msg["body"]).group(1) for f, r in rx.items()}
        assert got == {f: reference[f].search(msg["body"]).group(1) for f in rx}, msg["id"]
        if gt["outcome"] == "success":
            assert got == {k: str(v) for k, v in gt["body"].items()}, msg["id"]
        checked += 1
    assert checked == 52  # 53 acme messages, 1 of them junk


@pytest.mark.parametrize("domain", ["globex.com", "initech.com"])
def test_other_supplier_formats(domain):
    seed = generate()
    msg = next(m for m in seed.messages if m["sender_domain"] == domain
               and seed.ground_truth[m["id"]]["outcome"] == "success")
    cands = match(_with(msg, seed.ground_truth[msg["id"]]["body"]))
    assert cands["unexplained"] == []
    rx = {m["dest_field"]: re.compile(m["candidate_regex"]) for m in cands["mappings"]}
    for m in seed.messages:
        gt = seed.ground_truth[m["id"]]
        if m["sender_domain"] == domain and gt["outcome"] == "success":
            assert {f: r.search(m["body"]).group(1) for f, r in rx.items()} == \
                {k: str(v) for k, v in gt["body"].items()}


def test_normalises_case_whitespace_and_numbers():
    record = {"id": "r1", "body": "Customer:  ACME   Supply\nAmount: 40.0\nCount: 1,200\nCode: wid-101"}
    cands = match(_with(record, {"customer": "acme supply", "amount": 40, "count": "1200", "code": "WID-101"}))
    m = {x["dest_field"]: x for x in cands["mappings"]}
    assert cands["unexplained"] == []
    assert m["customer"]["label"] == "Customer:" and m["customer"]["matched"] == "ACME   Supply"
    assert m["amount"]["matched"] == "40.0" and m["amount"]["cast"] == "int"
    assert m["count"]["matched"] == "1,200" and m["count"]["cast"] is None  # it was sent as a string
    assert m["code"]["matched"] == "wid-101"
    assert all(x["regex_ok"] for x in m.values())


def test_whole_field_matches_are_json_paths_and_missing_values_are_unexplained():
    record = {"id": "r1", "sender_domain": "acme.com", "body": "Qty: 5"}
    cands = match(_with(record, {"supplier": "ACME.COM", "quantity": 5, "warehouse_id": "BOS", "n": 120}))
    m = {x["dest_field"]: x for x in cands["mappings"]}
    assert m["supplier"]["kind"] == "json" and m["supplier"]["json_path"] == "$.sender_domain"
    assert m["quantity"]["kind"] == "text"
    assert cands["unexplained"] == [{"dest_field": "warehouse_id", "value": "BOS"}, {"dest_field": "n", "value": 120}]


def test_numbers_inside_codes_do_not_match():
    record = {"id": "r1", "body": "SKU: WID-120\nPO: PO-1120\nDate 2026-11-02"}
    cands = match(_with(record, {"quantity": 120}))
    assert cands["unexplained"] == [{"dest_field": "quantity", "value": 120}]


def test_typed_values_are_the_backup_without_a_sink_request():
    rec = copy.deepcopy(FIXTURE)
    rec["events"] = [e for e in rec["events"] if not (e["type"] == "net" and e["method"] == "POST")]
    cands = match(rec)
    assert cands["sink"] is None
    m = {x["dest_field"]: x for x in cands["mappings"]}
    assert set(m) == {"product_id", "quantity", "reference"}
    assert m["quantity"]["value"] == "20" and m["quantity"]["label"] == "Qty:"


def test_no_source_means_everything_is_unexplained():
    rec = copy.deepcopy(FIXTURE)
    rec["events"] = [e for e in rec["events"] if not (e["type"] == "net" and e["method"] == "GET")]
    rec["events"] = [e for e in rec["events"] if e["type"] != "copy"]
    cands = match(rec)
    assert cands["source"] is None and cands["mappings"] == []
    assert [u["dest_field"] for u in cands["unexplained"]] == ["product_id", "quantity", "reference"]


def test_copied_text_explains_a_value_when_no_get_does():
    rec = copy.deepcopy(FIXTURE)
    rec["events"] = [e for e in rec["events"] if not (e["type"] == "net" and e["method"] == "GET")]
    cands = match(rec)
    m = {x["dest_field"]: x for x in cands["mappings"]}
    assert m["reference"]["kind"] == "copy" and m["reference"]["text"] == "PO-1022"


def test_helpers():
    assert char_class("GAD-217") == r"[A-Z]+-\d+"
    assert char_class("20") == r"\d+" and char_class("1,200.5") == r"\d[\d,]*(?:\.\d+)?"
    assert char_class("ab 12") == r"[a-z]+\s+\d+"
    assert left_label("Hi\nPO Number: PO-1", 13) == "PO Number:"
    assert left_label("ORDER REF=PO-1 | PART=X", 22) == "PART="
    assert label_regex("receive 20 units of item", "GAD-1") == r"receive\s+\d+\s+units\s+of\s+item\s*([A-Z]+-\d+)"


def test_cli_prints_candidates():
    out = subprocess.run([sys.executable, "-m", "matcher", str(REPO / "fixtures" / "recording.json")],
                         capture_output=True, text=True, cwd=REPO, check=True).stdout
    assert json.loads(out) == json.loads(json.dumps(match(FIXTURE)))


def test_matcher_is_pure():
    """No network, no LLM: the matcher imports only the standard library."""
    allowed = {"__future__", "re", "itertools", "typing", "urllib.parse", "argparse", "json", "sys", "pathlib",
               "matcher", "matcher.core",
               # v2 page -> form candidates (matcher.pages): the shared deterministic page helpers, still no network
               "matcher.pages", "datetime", "runner.html", "selectolax.lexbor"}
    for path in (REPO / "matcher").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module] if isinstance(node, ast.ImportFrom) else []
            for n in names:
                assert n in allowed, f"{path.name} imports {n}"
