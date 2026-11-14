"""Runner safety behaviour with a fake network: circuit breaker and quarantine threshold."""

import json

import pytest
from fastapi.testclient import TestClient

from compiler.compile import compile_spec, load_examples
from demo_apps.stockroom.app import create_app
from runner import run as run_mod
from runner.store import Store
from runner.testing import FakeHttp, FakeResponse
from spec import load_spec
from tests.conftest import FIXTURE_EXAMPLES, FIXTURE_SPEC


def _msg(i: int, domain: str, body: str) -> dict:
    return {"id": f"m{i:03d}", "sender": f"x@{domain}", "sender_domain": domain, "subject": "s",
            "body": body, "received_at": f"2026-11-02T08:{i:02d}:00Z"}


ACME = "PO Number: PO-{n}\nSKU: WID-101\nQty: 5"


class RunnerFake(FakeHttp):
    def __init__(self, openapi, **kw):
        super().__init__(**kw)
        self.openapi = openapi

    def request(self, target, method, path, *, params=None, json=None):
        if path == "/openapi.json":
            return FakeResponse(200, self.openapi)
        return super().request(target, method, path, params=params, json=json)

    def close(self):
        pass


@pytest.fixture
def deployed(workspace):
    openapi = TestClient(create_app()).get("/openapi.json").json()
    compile_spec(load_spec(FIXTURE_SPEC), version=1, out_root=workspace / "integrations",
                 examples=load_examples(FIXTURE_EXAMPLES), sink_openapi=openapi)
    store = Store()
    store.add_version("po_to_stockroom", 1, "h", None)
    store.set_tests("po_to_stockroom", 1, True, "ok")
    store.set_active("po_to_stockroom", 1)
    return store, openapi


def _run_with(monkeypatch, store, fake, **kw):
    monkeypatch.setattr(run_mod, "Http", lambda config, staging=False: fake)
    return run_mod.run("po_to_stockroom", store=store, log=lambda s: None, **kw)


def test_circuit_breaker_stops_after_three_sink_contract_failures(monkeypatch, deployed):
    store, openapi = deployed
    records = [_msg(i, "acme.com", ACME.format(n=1000 + i)) for i in range(10)]
    fake = RunnerFake(openapi, records=records, sink_status=422)
    summary = _run_with(monkeypatch, store, fake)
    assert summary["status"] == "tripped"
    assert summary["counts"]["quarantined:SINK_CONTRACT"] == 3
    assert len(fake.sink_writes) == 3  # stopped; the other 7 were never attempted
    assert store.incident(summary["incident_id"])["type"] == "CIRCUIT_BREAKER"


def test_new_source_format_opens_an_incident(monkeypatch, deployed):
    store, openapi = deployed
    new_format = "Item code: WID-101 / 2 cases (12/case) for order PO-{n}"
    records = [_msg(i, "initech.com", new_format.format(n=3000 + i)) for i in range(4)]
    records.append(_msg(10, "acme.com", ACME.format(n=1010)))
    fake = RunnerFake(openapi, records=records)
    summary = _run_with(monkeypatch, store, fake)
    assert summary["status"] == "completed"
    assert summary["counts"]["success"] == 1
    assert summary["counts"]["quarantined:PARSE_NO_MATCH"] == 4
    incident = store.incident(summary["incident_id"])
    assert incident["type"] == "NEW_SOURCE_FORMAT"
    assert "initech.com" in incident["summary"] and len(incident["samples"]) == 4


def test_records_are_idempotent_across_runs(monkeypatch, deployed):
    store, openapi = deployed
    records = [_msg(1, "acme.com", ACME.format(n=1001))]
    fake = RunnerFake(openapi, records=records)
    _run_with(monkeypatch, store, fake)
    store.set_cursor("po_to_stockroom", "live", "")  # force a replay of the same record
    second = _run_with(monkeypatch, store, fake)
    assert second["counts"] == {"duplicate": 1}
    assert len(fake.sink_writes) == 1


def test_run_log_is_jsonl_with_llm_calls_zero(monkeypatch, deployed, workspace):
    store, openapi = deployed
    fake = RunnerFake(openapi, records=[_msg(1, "acme.com", ACME.format(n=1001))])
    summary = _run_with(monkeypatch, store, fake)
    assert summary["llm_calls"] == 0
    lines = [json.loads(ln) for ln in (workspace / "runs" / f"{summary['run_id']}.jsonl").read_text().splitlines()]
    assert lines[0]["event"] == "preflight" and lines[1]["event"] == "record"
