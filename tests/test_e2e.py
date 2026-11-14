"""MVP acceptance: fixture spec -> compile v1 -> tests pass -> approve -> run all 150 seeded
messages with zero LLM calls and exact accuracy -> Break 1 blocked before any write."""

import httpx

from bench.accuracy import score
from control import ops
from runner.store import Store
from tests.conftest import FIXTURE_SPEC


def _incoming(url: str) -> list[dict]:
    return httpx.get(url + "/api/incoming").json()


def test_mvp_end_to_end(apps):
    built = ops.compile_and_test(FIXTURE_SPEC)
    assert built["tests_passed"], built["tests_summary"]
    ops.deploy("po_to_stockroom", 1)

    dry = ops.run_sandboxed("po_to_stockroom", dry_run=True, limit=10)
    assert dry["status"] == "completed" and dry["llm_calls"] == 0
    assert _incoming(apps["live"]) == [], "a dry run must never write to the live sink"
    assert len(_incoming(apps["staging"])) > 0

    first = ops.run_sandboxed("po_to_stockroom", limit=100)
    rest = ops.run_sandboxed("po_to_stockroom")
    again = ops.run_sandboxed("po_to_stockroom")  # cursor: nothing new
    for s in (first, rest, again):
        assert s["status"] == "completed", s
        assert s["llm_calls"] == 0
    assert sum(first["counts"].get(k, 0) for k in ("success", "duplicate", "quarantined")) == 100
    assert again["counts"] == {}

    store = Store()
    records = store.records(first["run_id"]) + store.records(rest["run_id"])
    result = score(records)
    assert result["records"] == 150
    assert result["accuracy"] == 1.0, result["mismatches"]
    assert len(_incoming(apps["live"])) == 120  # one write per unique valid PO

    # Break 1: destination contract changes. Preflight must block before any write.
    httpx.post(apps["live"] + "/admin/schema", json={"version": 2})
    before = len(_incoming(apps["live"]))
    blocked = ops.run_sandboxed("po_to_stockroom")
    assert blocked["status"] == "blocked" and blocked["exit_code"] == 2
    assert len(_incoming(apps["live"])) == before
    incident = store.incident(blocked["incident_id"])
    assert incident["type"] == "CONTRACT_CHANGED"
    errors = "\n".join(incident["detail"]["contract_errors"])
    assert "warehouse_id: required" in errors
    assert incident["old_openapi"] != incident["new_openapi"]
