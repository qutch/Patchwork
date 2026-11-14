"""Delivered messages through the real v1 integration: ground truth from /admin/ground_truth is
exactly what the runner does, and Break 2 messages open a NEW_SOURCE_FORMAT incident."""

from __future__ import annotations

import httpx

from bench.accuracy import score
from control import ops
from runner.store import Store
from tests.conftest import FIXTURE_SPEC


def test_v1_on_delivered_and_new_supplier_messages(apps):
    built = ops.compile_and_test(FIXTURE_SPEC)
    assert built["tests_passed"], built["tests_summary"]
    ops.deploy("po_to_stockroom", 1)
    seeded = ops.run_sandboxed("po_to_stockroom")
    assert seeded["status"] == "completed" and seeded["incident_id"] is None

    inbox = apps["inbox"]
    httpx.post(inbox + "/admin/deliver", json={"count": 50}).raise_for_status()
    httpx.post(inbox + "/admin/new_supplier", json={"count": 4}).raise_for_status()
    delivered = ops.run_sandboxed("po_to_stockroom")
    assert delivered["status"] == "completed" and delivered["llm_calls"] == 0
    assert delivered["counts"]["quarantined:PARSE_NO_MATCH"] == 5  # 4 umbrella + 1 junk

    store = Store()
    incident = store.incident(delivered["incident_id"])
    assert incident["type"] == "NEW_SOURCE_FORMAT"
    assert "umbrella.com" in incident["summary"] and len(incident["samples"]) == 4
    assert all("Item code:" in s["record"]["body"] for s in incident["samples"])

    truth = httpx.get(inbox + "/admin/ground_truth").json()
    assert len(truth) == 204
    records = store.records(seeded["run_id"]) + store.records(delivered["run_id"])
    result = score(records, truth)
    assert result["records"] == 204
    assert result["accuracy"] == 1.0, result["mismatches"]
