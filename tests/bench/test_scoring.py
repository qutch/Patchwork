"""bench.scoring: write-level scoring used for both Patchwork and the one-shot baseline script."""

from bench.scoring import baseline_flagged, crashed, enrich_truth, score_writes
from demo_apps.seed import generate, new_supplier_messages

OK = {"outcome": "success", "reason": None,
      "body": {"product_id": "WID-101", "quantity": 5, "reference": "PO-1"},
      "body_v2": {"sku": "WID-101", "quantity_delta": 5, "reference": "PO-1", "warehouse_id": "BOS"}}
OK2 = {"outcome": "success", "reason": None,
       "body": {"product_id": "GAD-200", "quantity": 7, "reference": "PO-2"},
       "body_v2": {"sku": "GAD-200", "quantity_delta": 7, "reference": "PO-2", "warehouse_id": "NYC"}}
RESEND = {"outcome": "duplicate", "reason": None, "reference": "PO-1"}
ZERO = {"outcome": "quarantined", "reason": "VALIDATION_FAILED", "reference": "PO-9"}
JUNK = {"outcome": "quarantined", "reason": "PARSE_NO_MATCH"}


def row(i, body, v=1):
    return {"id": i, "schema_version": v, "created_at": "2026-11-14T00:00:00Z", **body}


def test_all_right():
    truth = {"m1": OK, "m2": OK2, "m3": RESEND, "m4": ZERO, "m5": JUNK}
    s = score_writes(truth, truth, [row(1, OK["body"]), row(2, OK2["body_v2"], 2)])
    assert (s["correct_writes"], s["wrong_writes"], s["silently_dropped"], s["held"]) == (2, 0, 0, 0)
    assert s["accuracy"] == 1.0


def test_resend_written_twice_is_a_wrong_write_and_marks_the_resend_wrong():
    truth = {"m1": OK, "m3": RESEND}
    s = score_writes(truth, truth, [row(1, OK["body"]), row(2, OK["body"])])
    assert s["correct_writes"] == 1 and s["wrong_writes"] == 1
    assert s["examples"]["wrong"][0]["why"].startswith("second row")
    assert s["correct_records"] == 1  # the original is right, the resend is not


def test_resend_of_a_po_written_in_an_earlier_phase():
    truth = {"m3": RESEND}
    s = score_writes(truth, ["m3"], [row(5, OK["body"])], prior_rows=[row(1, OK["body"])])
    assert s["wrong_writes"] == 1 and s["accuracy"] == 0.0


def test_wrong_body_and_should_not_be_written():
    truth = {"m1": OK, "m4": ZERO}
    bad = dict(OK["body"], quantity=50)
    s = score_writes(truth, truth, [row(1, bad), row(2, {"product_id": "WID-101", "quantity": 0, "reference": "PO-9"})])
    assert s["correct_writes"] == 0 and s["wrong_writes"] == 2
    assert {w["why"] for w in s["examples"]["wrong"]} == {"wrong body", "should not have been written"}
    assert s["silently_dropped"] == 0  # a wrongly written record is a wrong write, not also a drop
    assert s["accuracy"] == 0.0


def test_missing_records_are_held_when_flagged_else_silently_dropped():
    truth = {"m1": OK, "m2": OK2}
    s = score_writes(truth, truth, [], flagged={"m1"})
    assert s["held"] == 1 and s["silently_dropped"] == 1
    assert s["examples"]["dropped"] == [{"record_id": "m2", "reference": "PO-2"}]


def test_new_supplier_case_math():
    batch = new_supplier_messages(0, 1, after="2026-11-03T00:00:00Z", first_id=151)
    (mid, gt), = batch.ground_truth.items()
    truth = {mid: gt}
    right = score_writes(truth, [mid], [row(1, gt["intended"]["body"])])
    assert right["correct_writes"] == 1 and right["accuracy"] == 1.0
    cases_not_units = dict(gt["intended"]["body"], quantity=gt["intended"]["cases"])
    if gt["intended"]["per_case"] != 1:
        wrong = score_writes(truth, [mid], [row(1, cases_not_units)])
        assert wrong["wrong_writes"] == 1 and wrong["accuracy"] == 0.0
    held = score_writes(truth, [mid], [], flagged=[mid])
    assert held["held"] == 1 and held["accuracy"] == 1.0  # quarantined until a repair: right
    dropped = score_writes(truth, [mid], [])
    assert dropped["silently_dropped"] == 1 and dropped["accuracy"] == 0.0


def test_enrich_truth_reads_the_po_from_resend_subjects():
    seed = generate()
    truth = enrich_truth(seed.ground_truth, seed.messages)
    resends = [m for m in seed.messages if seed.ground_truth[m["id"]]["outcome"] == "duplicate"]
    assert resends and all(truth[m["id"]]["reference"] in m["subject"] for m in resends)
    junk = [k for k, v in seed.ground_truth.items() if v["reason"] == "PARSE_NO_MATCH"]
    assert all("reference" not in truth[k] for k in junk)
    assert "reference" not in seed.ground_truth[resends[0]["id"]]  # input not mutated


def test_seed_scored_against_its_own_truth_is_perfect():
    seed = generate()
    truth = enrich_truth(seed.ground_truth, seed.messages)
    rows = [row(i, gt["body"]) for i, gt in enumerate(seed.ground_truth.values()) if gt["outcome"] == "success"]
    s = score_writes(truth, truth, rows)
    assert (s["records"], s["correct_writes"], s["accuracy"]) == (150, 120, 1.0)


def test_baseline_flagged_and_crashed():
    state = {"messages": {"a": {"status": "recorded"}, "b": {"status": "error"}, "c": {"status": "not_po"},
                          "d": {"status": "unparsed"}, "e": {"status": "unknown_product"}}}
    assert baseline_flagged(state) == {"b", "d", "e"}
    assert baseline_flagged({}) == set()
    assert not crashed(0, "") and not crashed(1, "ERROR msg-1 failed")
    assert crashed(2, "") and crashed(1, "Traceback (most recent call last):\n ...") and crashed(None, "")
