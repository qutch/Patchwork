"""contracts V18: outcomes are reported per document, not per row id. `<id>` and `<id>#<n>` are one document, the
document's latest run wins, and a one-record page reads exactly as before."""

from __future__ import annotations

import pytest

from runner.pipeline import Outcome
from runner.store import Store, document_key, summarize_document


def out(rid: str, status: str = "success", reason: str | None = None, detail: str | None = None, **kw) -> Outcome:
    return Outcome(record_id=rid, status=status, reason=reason, detail=detail, parser="p", route_value="s",
                   key=rid, body=kw.pop("body", {"N": rid}), **kw)


def row(rid: str, status: str = "success", run_id: int = 1, **kw) -> dict:
    return {"record_id": rid, "status": status, "reason": kw.get("reason"), "detail": kw.get("detail"),
            "run_id": run_id, "sink_body": kw.get("body"), "parser": "p"}


@pytest.mark.parametrize("rid,want", [("pg-000002#3", "pg-000002"), ("doc-000001", "doc-000001"), ("a#b#12", "a#b"),
                                      ("x#", "x#"), ("#7", "#7"), ("v1-record-5", "v1-record-5"), ("pg-1#x", "pg-1#x")])
def test_document_key(rid, want):
    assert document_key(rid) == want


def test_a_plain_record_is_returned_unchanged():
    r = row("msg-1", "quarantined", reason="PARSE_NO_MATCH", detail="no parser")
    assert summarize_document([r]) == r and "rows" not in summarize_document([r])


def test_rows_all_written_are_one_success_with_counts():
    s = summarize_document([row("d#2", body={"N": 2}), row("d#1", body={"N": 1}), row("d#3", "duplicate")])
    assert (s["record_id"], s["status"], s["reason"]) == ("d", "success", None)
    assert s["rows"] == {"success": 2, "duplicate": 1, "quarantined": 0} and s["sink_body"] == {"N": 1}


def test_every_row_a_duplicate_is_a_duplicate_and_one_held_row_holds_the_document():
    assert summarize_document([row("d#1", "duplicate"), row("d#2", "duplicate")])["status"] == "duplicate"
    held = summarize_document([row("d#1"), row("d#2", "quarantined", reason="SINK_CONTRACT", detail="422"),
                               row("d#3")])
    assert held["status"] == "quarantined" and held["reason"] == "SINK_CONTRACT" and held["detail"] == "row 2: 422"
    assert held["rows"] == {"success": 2, "duplicate": 0, "quarantined": 1}


def test_a_one_row_document_is_still_a_document():
    s = summarize_document([row("d#1")])
    assert s["record_id"] == "d" and s["rows"] == {"success": 1, "duplicate": 0, "quarantined": 0}


def run(store: Store, records: list[tuple[Outcome, dict]], *, dry_run: bool = False) -> int:
    rid = store.start_run("n", 1, dry_run, kind="run", pid=1)
    for o, raw in records:
        store.add_record(rid, o, raw)
    store.finish_run(rid, "completed", {}, 0.1, None, None)
    return rid


def test_latest_run_of_a_document_wins_in_both_directions(workspace):
    store = Store()
    doc = {"id": "pg-1"}
    run(store, [(out("pg-1", "quarantined", "PARSE_NO_MATCH"), doc)])  # held as a whole
    run(store, [(out("pg-1#1"), doc), (out("pg-1#2"), doc)])  # then its rows are written
    assert [r["record_id"] for r in store.latest_outcomes_by_document("n")["pg-1"]] == ["pg-1#1", "pg-1#2"]
    assert store.quarantined_raw("n", "id") == []  # nothing left to replay
    (one,) = store.latest_document_outcomes("n")
    assert one["record_id"] == "pg-1" and one["status"] == "success" and one["rows"]["success"] == 2
    run(store, [(out("pg-1", "quarantined", "VALIDATION_FAILED", "sum_equals"), doc)])  # later held again
    assert [r["record_id"] for r in store.latest_outcomes_by_document("n")["pg-1"]] == ["pg-1"]
    assert store.quarantined_raw("n", "id") == [doc]
    assert store.latest_document_outcomes("n")[0]["status"] == "quarantined"


def test_dry_runs_and_other_documents_do_not_mix(workspace):
    store = Store()
    run(store, [(out("a#1"), {"id": "a"}), (out("b", "quarantined", "PARSE_NO_MATCH"), {"id": "b"})])
    run(store, [(out("a#1", "quarantined", "UNKNOWN"), {"id": "a"})], dry_run=True)
    by = {o["record_id"]: o["status"] for o in store.latest_document_outcomes("n")}
    assert by == {"a": "success", "b": "quarantined"}
    assert store.quarantined_raw("n", "id") == [{"id": "b"}]
