"""Exact accuracy for a run against the seed's ground truth.

  python -m bench.accuracy <run_id> [<run_id> ...]
  python -m bench.accuracy --live po_to_stockroom     # every live run, latest outcome per record
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from demo_apps.seed import generate
from runner.store import Store


def score(records: list[dict], truth: dict[str, dict] | None = None) -> dict[str, Any]:
    truth = truth or generate().ground_truth
    right, wrong = 0, []
    for r in records:
        gt = truth.get(r["record_id"])
        if gt is None:
            wrong.append({"record_id": r["record_id"], "why": "not in ground truth"})
            continue
        ok = r["status"] == gt["outcome"] and (r["reason"] or None) == gt["reason"]
        if ok and gt["outcome"] == "success":
            expected = gt["body_v2"] if r["sink_body"] and "sku" in r["sink_body"] else gt["body"]
            ok = r["sink_body"] == expected
        if ok:
            right += 1
        else:
            wrong.append({"record_id": r["record_id"], "got": [r["status"], r["reason"], r["sink_body"]],
                          "expected": [gt["outcome"], gt["reason"], gt.get("body")]})
    n = len(records)
    return {"records": n, "correct": right, "accuracy": round(right / n, 4) if n else None,
            "mismatches": wrong[:20]}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bench.accuracy")
    p.add_argument("run_ids", nargs="*", type=int)
    p.add_argument("--live", metavar="INTEGRATION")
    args = p.parse_args(argv)
    store = Store()
    run_ids = args.run_ids
    if args.live:
        run_ids = [r["id"] for r in reversed(store.runs(args.live, limit=10_000)) if not r["dry_run"]]
    latest: dict[str, dict] = {}
    for rid in run_ids:
        for rec in store.records(rid):
            latest[rec["record_id"]] = rec
    result = score(list(latest.values()))
    print(json.dumps(result, indent=2))
    return 0 if result["accuracy"] == 1.0 else 1


if __name__ == "__main__":
    sys.exit(main())
