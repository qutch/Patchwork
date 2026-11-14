"""python -m capture record <dir> --intent "..." --allow localhost:8001 --allow localhost:8002
                          [--start-url http://localhost:8001/] [--headless]"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m capture")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="launch Chromium and record a demonstration")
    r.add_argument("dir", type=Path, help="recording directory, e.g. workspace/recordings/rec-...")
    r.add_argument("--intent", required=True)
    r.add_argument("--allow", action="append", default=[], metavar="HOST:PORT",
                   help="host to record (repeatable); nothing else is captured")
    r.add_argument("--start-url")
    r.add_argument("--headless", action="store_true")
    args = p.parse_args(argv)
    if not args.allow:
        p.error("at least one --allow host:port is required")
    from capture.launcher import record
    asyncio.run(record(args.dir.resolve(), args.intent, args.allow, args.start_url, args.headless))
    return 0


if __name__ == "__main__":
    sys.exit(main())
