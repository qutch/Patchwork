"""bench.run end to end on its own in-process stack (scripted agent, free ports), results labelling,
the stopwatch and the /api/bench/results endpoint."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bench import run as bench_run
from bench.stopwatch import apply_timings, main as stopwatch_main, time_transfers

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def measured(tmp_path_factory):
    ws = tmp_path_factory.mktemp("bench")
    return bench_run.measure_patchwork(ws / "pw"), bench_run.measure_baseline(ws / "bl")


def test_patchwork_catches_both_breaks_before_any_bad_write(measured):
    pw, _ = measured
    assert pw["run"]["records"] == 100 and pw["run"]["llm_calls"] == 0 and pw["run"]["accuracy"] == 1.0
    assert pw["seed"]["accuracy"] == 1.0
    b1, b2 = pw["break1"], pw["break2"]
    assert b1["status"] == "blocked" and b1["incident_type"] == "CONTRACT_CHANGED"
    assert b1["writes"]["wrong_writes"] == 0 and b1["writes"]["held"] == b1["writes"]["expected_writes"]
    assert b2["incident_type"] == "NEW_SOURCE_FORMAT" and b2["quarantined"] == 4
    assert b2["writes"]["wrong_writes"] == 0 and b2["writes"]["silently_dropped"] == 0
    assert b1["accuracy_after"] == 1.0 and b2["accuracy_after"] == 1.0 and b2["records_after"] == 159


def test_baseline_outcomes(measured):
    _, bl = measured
    assert bl["run"]["writes"]["correct_writes"] == 120 and bl["run"]["writes"]["wrong_writes"] == 0
    assert not bl["run"]["crashed"]
    assert bl["break1"]["writes"]["wrong_writes"] == 0  # the Stockroom's own validation rejects v1 bodies
    assert bl["break2"]["writes"]["silently_dropped"] == 4
    assert set(bl["break2"]["statuses"].values()) == {"not_po"}


def test_results_are_fully_labelled(measured):
    pw, bl = measured
    prior = {"manual": {"timings_s": [50, 70]}}
    r = bench_run.build_results(pw, bl, prior, generated_at="2026-11-14T00:00:00Z")
    assert bench_run.check_labels(r) == []
    assert r["summary"]["caught_before_bad_write"] == {"patchwork": 2, "baseline_script": 1}
    assert r["patchwork"]["deep_agent"]["build_s"]["kind"] == "measured (earlier run)"
    assert r["manual"]["per_record_min"]["kind"] == "measured" and r["manual"]["per_record_min"]["value"] == 1.0
    assert r["computer_use"]["per_record_min"]["kind"] == "estimate"
    bb = r["baseline_script"]["breaks"]
    assert bb["break2"]["detected"] is False and bb["break2"]["time_to_detect_s"]["value"] is None
    assert bb["break1"]["repair_s"]["kind"] == "not measured"
    json.dumps(r)  # serialisable


def test_check_labels_finds_bare_numbers():
    assert bench_run.check_labels({"patchwork": {"x": 3}}) == [".patchwork.x"]
    assert bench_run.check_labels({"a": {"value": 1, "unit": "s", "kind": "guess", "source": "me"}}) == [".a"]
    assert bench_run.check_labels({"summary": {"n": 2}, "a": bench_run.metric(1, "s", "estimate", "x")}) == []
    with pytest.raises(AssertionError):
        bench_run.metric(None, "s", "measured", "x")  # a measurement needs a value


def test_committed_results_are_labelled():
    r = json.loads((REPO / "bench" / "results.json").read_text())
    assert r["schema_version"] == 1 and bench_run.check_labels(r) == []


def test_stopwatch_apply_and_time(tmp_path):
    r = apply_timings({"manual": {"timings_s": []}}, "manual", [60, 90])
    assert r["manual"]["per_record_min"] == {**r["manual"]["per_record_min"], "value": 1.25, "kind": "measured"}
    back = apply_timings(r, "manual", [])
    assert back["manual"]["per_record_min"]["kind"] == "estimate"
    ticks = iter([0.0, 42.0, 100.0, 155.5])
    assert time_transfers(2, "x", prompt=lambda _: "", clock=lambda: next(ticks)) == [42.0, 55.5]

    path = tmp_path / "results.json"
    path.write_text(json.dumps({"keep": 1}))
    assert stopwatch_main(["manual", "--add", "30", "90", "--results", str(path)]) == 0
    assert stopwatch_main(["computer_use", "--add", "240", "--results", str(path)]) == 0
    out = json.loads(path.read_text())
    assert out["keep"] == 1 and out["manual"]["timings_s"] == [30, 90]
    assert out["computer_use"]["per_record_min"]["value"] == 4.0
    assert stopwatch_main(["manual", "--add", "60", "--results", str(path)]) == 0
    assert json.loads(path.read_text())["manual"]["timings_s"] == [30, 90, 60]


def test_bench_results_endpoint(tmp_path, monkeypatch, workspace):
    from control.api import create_app

    client = TestClient(create_app(agent=object(), dotenv=False))
    monkeypatch.setenv("PATCHWORK_BENCH_RESULTS", str(tmp_path / "missing.json"))
    r = client.get("/api/bench/results")
    assert r.status_code == 404 and "make bench" in r.json()["detail"]
    (tmp_path / "r.json").write_text(json.dumps({"schema_version": 1}))
    monkeypatch.setenv("PATCHWORK_BENCH_RESULTS", str(tmp_path / "r.json"))
    assert client.get("/api/bench/results").json() == {"schema_version": 1}
    monkeypatch.delenv("PATCHWORK_BENCH_RESULTS")
    assert client.get("/api/bench/results").json()["schema_version"] == 1  # the committed file
