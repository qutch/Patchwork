"""Write-level scoring against ground truth, the same for Patchwork and the one-shot baseline script.

`bench.accuracy.score` scores Patchwork's own per-record outcomes. A plain script has no such
outcomes, so this module looks at what actually landed in the Stockroom (its rows) plus which
messages the system *flagged* to a human (Patchwork: quarantined/held with an incident; the
script: logged at ERROR and left for retry). Pure functions, no I/O.

Per message in scope, by ground truth:
  success      right if one row with the expected body was written (v1 or v2 body)
  duplicate    right if no second row was written for that reference (a resend)
  quarantined  right if nothing was written (zero qty, unknown SKU, junk)
  new supplier (Break 2, has `intended`): right if written with the intended body, or held and
               flagged; wrong if written with another body; silently dropped if neither
Every row written in scope is either a correct write or a wrong write (wrong body, second row
for a reference, or a row for a message that should not have been written).
"""

from __future__ import annotations

import re
from typing import Any, Iterable

ROW_META = ("id", "schema_version", "created_at")
PO_IN_SUBJECT = re.compile(r"PO-\d+")


def row_body(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in ROW_META}


def _expected_bodies(gt: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Bodies a correct write may have for this message, or None if it must not be written."""
    if gt.get("intended"):
        return [gt["intended"]["body"], gt["intended"]["body_v2"]]
    if gt.get("outcome") == "success":
        return [gt["body"], gt["body_v2"]]
    return None


def _reference(gt: dict[str, Any]) -> str | None:
    bodies = _expected_bodies(gt)
    return bodies[0]["reference"] if bodies else gt.get("reference")


def score_writes(truth: dict[str, dict], message_ids: Iterable[str], new_rows: list[dict],
                 prior_rows: Iterable[dict] = (), flagged: Iterable[str] = ()) -> dict[str, Any]:
    """Score one phase. `new_rows` = Stockroom rows written during the phase (any order; sorted
    by id), `prior_rows` = rows that were already there, `flagged` = message ids the system
    surfaced to a human. Returns counts plus up to 20 examples of each problem."""
    ids = list(message_ids)
    flagged = set(flagged)
    expected: dict[str, tuple[str, list[dict]]] = {}  # reference -> (message id, acceptable bodies)
    for mid in ids:
        gt = truth.get(mid)
        bodies = _expected_bodies(gt) if gt else None
        if bodies:
            expected.setdefault(bodies[0]["reference"], (mid, bodies))
    seen = {row_body(r).get("reference") or None for r in prior_rows}
    seen.discard(None)
    correct_refs: set[str] = set()
    wrong: list[dict] = []
    bad_refs: set[str] = set()
    for row in sorted(new_rows, key=lambda r: r.get("id", 0)):
        body = row_body(row)
        ref = body.get("reference")
        if ref in expected and ref not in seen and body in expected[ref][1]:
            correct_refs.add(ref)
        else:
            why = ("second row for a reference (resend written again)" if ref in seen
                   else "wrong body" if ref in expected else "should not have been written")
            wrong.append({"reference": ref, "body": body, "why": why,
                          "expected": expected[ref][1][0] if ref in expected else None})
            bad_refs.add(ref)
        seen.add(ref)

    dropped, held = [], []
    for ref, (mid, _) in expected.items():
        if ref in correct_refs or ref in bad_refs:
            continue
        (held if mid in flagged else dropped).append({"record_id": mid, "reference": ref})

    # message-level accuracy
    right = 0
    for mid in ids:
        gt = truth.get(mid)
        if gt is None:
            continue
        ref = _reference(gt)
        if gt.get("intended"):
            ok = ref in correct_refs or (ref not in bad_refs and mid in flagged)
        elif gt.get("outcome") == "success":
            ok = ref in correct_refs
        else:  # resend / zero qty / unknown SKU / junk: right if no (extra) row was written for it
            ok = ref is None or ref not in bad_refs
        right += ok

    n = len(ids)
    return {
        "records": n,
        "correct_writes": len(correct_refs),
        "wrong_writes": len(wrong),
        "silently_dropped": len(dropped),
        "held": len(held),
        "expected_writes": len(expected),
        "correct_records": right,
        "accuracy": round(right / n, 4) if n else None,
        "examples": {"wrong": wrong[:20], "dropped": dropped[:20], "held": held[:20]},
    }


def enrich_truth(truth: dict[str, dict], messages: Iterable[dict]) -> dict[str, dict]:
    """Ground truth has no body for resends and quarantined POs. Add their PO `reference` (read
    from the subject, e.g. "RESEND: [ERP] PO-3030") so a row written for them can be attributed."""
    out = {k: dict(v) for k, v in truth.items()}
    for m in messages:
        gt = out.get(m["id"])
        if gt is not None and not _expected_bodies(gt) and "reference" not in gt:
            hit = PO_IN_SUBJECT.search(m.get("subject") or "")
            if hit:
                gt["reference"] = hit.group(0)
    return out


def baseline_flagged(state: dict[str, Any]) -> set[str]:
    """Messages the baseline script logged at ERROR and left for retry (its only alert)."""
    msgs = (state or {}).get("messages") or {}
    return {mid for mid, s in msgs.items() if isinstance(s, dict) and s.get("status") in
            ("unparsed", "unknown_product", "error")}


def crashed(exit_code: int | None, stderr: str) -> bool:
    """A crash, as opposed to the script's own 'problems found' exit code 1."""
    return exit_code is None or exit_code not in (0, 1) or "Traceback (most recent call last)" in (stderr or "")
