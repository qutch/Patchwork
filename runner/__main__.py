"""python -m runner run <name> [--version active|N] [--dry-run] [--limit N]

Exit codes: 0 completed, 2 blocked or tripped (incident opened), 1 failed.
"""

from __future__ import annotations

import argparse
import sys

from runner.run import run


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="runner")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run an integration (no LLM)")
    r.add_argument("name")
    r.add_argument("--version", default="active")
    r.add_argument("--dry-run", action="store_true", help="target the staging sink")
    r.add_argument("--limit", type=int)
    args = p.parse_args(argv)
    summary = run(args.name, args.version, dry_run=args.dry_run, limit=args.limit)
    return {"completed": 0, "blocked": 2, "tripped": 2}.get(summary["status"], 1)


if __name__ == "__main__":
    sys.exit(main())
