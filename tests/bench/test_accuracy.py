"""bench.accuracy.score: a Break 2 record is right when quarantined before the repair, and right
when it succeeds afterwards with the body a correct repair would send."""

from bench.accuracy import score
from demo_apps.seed import generate, new_supplier_messages


def _new_supplier_truth():
    seed = generate()
    batch = new_supplier_messages(0, 1, after="2026-11-03T00:00:00Z", first_id=len(seed.messages) + 1)
    [(mid, gt)] = batch.ground_truth.items()
    return mid, gt


def rec(mid, status, reason=None, body=None):
    return {"record_id": mid, "status": status, "reason": reason, "sink_body": body}


def test_new_supplier_record_is_right_quarantined_before_repair():
    mid, gt = _new_supplier_truth()
    assert score([rec(mid, "quarantined", "PARSE_NO_MATCH")], {mid: gt})["accuracy"] == 1.0


def test_new_supplier_record_is_right_when_repaired_with_the_intended_body():
    mid, gt = _new_supplier_truth()
    truth = {mid: gt}
    assert score([rec(mid, "success", body=gt["intended"]["body_v2"])], truth)["accuracy"] == 1.0
    assert score([rec(mid, "success", body=gt["intended"]["body"])], truth)["accuracy"] == 1.0
    wrong = dict(gt["intended"]["body_v2"], quantity_delta=gt["intended"]["cases"])  # cases, not units
    result = score([rec(mid, "success", body=wrong)], truth)
    assert result["accuracy"] == 0.0 and result["mismatches"][0]["expected"][2] == gt["intended"]["body_v2"]


def test_ordinary_quarantine_truth_is_unchanged():
    seed = generate()
    mid, gt = next((k, v) for k, v in seed.ground_truth.items() if v["outcome"] == "quarantined")
    assert score([rec(mid, "success", body={"x": 1})], seed.ground_truth)["accuracy"] == 0.0
