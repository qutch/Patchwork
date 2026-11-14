"""Time a few manual (or computer-use agent) transfers and store them in bench/results.json.

    python -m bench.stopwatch manual              # time 5 transfers: Enter to start, Enter when saved
    python -m bench.stopwatch computer_use -n 5   # time 5 agent runs the same way
    python -m bench.stopwatch manual --add 48 61.5 55   # record timings (seconds) taken elsewhere
    python -m bench.stopwatch manual --clear      # back to the labelled estimate

One transfer = open a purchase order in the Supplier Inbox, enter it in the Stockroom's
"Add incoming stock" form, save. The per-record figure becomes the mean of the timings, labelled
`measured`; the benchmark page extrapolates it (labelled) to more records. `make bench` keeps
these timings when it regenerates results.json.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Callable

from bench.run import (COMPUTER_USE_ASSUMPTION, COMPUTER_USE_ESTIMATE_MIN, COMPUTER_USE_SOURCE,
                       MANUAL_ASSUMPTION, MANUAL_ESTIMATE_MIN, RESULTS, timing_metric)

WHAT = {"manual": ("a manual transfer", MANUAL_ESTIMATE_MIN, MANUAL_ASSUMPTION, "assumption (not timed yet)"),
        "computer_use": ("a computer-use agent run", COMPUTER_USE_ESTIMATE_MIN, COMPUTER_USE_ASSUMPTION,
                         COMPUTER_USE_SOURCE)}


def apply_timings(results: dict, which: str, timings_s: list[float]) -> dict:
    """Store `timings_s` under results[which] and recompute its per_record_min metric."""
    _, est, assumption, est_source = WHAT[which]
    section = dict(results.get(which) or {})
    section["timings_s"] = [round(float(t), 2) for t in timings_s]
    section["per_record_min"] = timing_metric(section["timings_s"], est, f"bench/stopwatch.py ({which})",
                                              assumption, est_source)
    out = dict(results)
    out[which] = section
    return out


def time_transfers(n: int, label: str, prompt: Callable[[str], str] = input,
                   clock: Callable[[], float] = time.monotonic) -> list[float]:
    out = []
    for i in range(1, n + 1):
        prompt(f"[{i}/{n}] press Enter to START {label} ")
        t = clock()
        prompt(f"[{i}/{n}] press Enter when it is SAVED ")
        out.append(clock() - t)
        print(f"        {out[-1]:.1f} s")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bench.stopwatch", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("which", choices=sorted(WHAT))
    ap.add_argument("-n", type=int, default=5, help="how many transfers to time (default 5)")
    ap.add_argument("--add", type=float, nargs="+", metavar="SECONDS", help="record these timings instead of timing")
    ap.add_argument("--clear", action="store_true", help="drop recorded timings (back to the estimate)")
    ap.add_argument("--replace", action="store_true", help="replace earlier timings instead of appending")
    ap.add_argument("--results", type=Path, default=RESULTS)
    args = ap.parse_args(argv)

    results = json.loads(args.results.read_text()) if args.results.exists() else {}
    old = [] if (args.clear or args.replace) else list((results.get(args.which) or {}).get("timings_s") or [])
    if args.clear:
        new: list[float] = []
    elif args.add:
        new = list(args.add)
    else:
        try:
            new = time_transfers(args.n, WHAT[args.which][0])
        except (KeyboardInterrupt, EOFError):
            print("\naborted; nothing written")
            return 1
    results = apply_timings(results, args.which, old + new)
    args.results.write_text(json.dumps(results, indent=2) + "\n")
    m = results[args.which]["per_record_min"]
    print(f"{args.which}: {len(results[args.which]['timings_s'])} timings -> {m['value']} min/record ({m['kind']})"
          f" in {args.results}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
