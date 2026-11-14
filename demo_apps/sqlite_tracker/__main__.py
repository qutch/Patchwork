"""python -m demo_apps.sqlite_tracker [--db workspace/tracker.db] (--init | --rename OLD NEW | --rows)"""

from __future__ import annotations

import argparse
import json

from demo_apps.sqlite_tracker.db import create_db, rename_column, rows


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="demo_apps.sqlite_tracker")
    p.add_argument("--db", default="workspace/tracker.db")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--init", action="store_true", help="create (or reset) the Applications table")
    g.add_argument("--rename", nargs=2, metavar=("OLD", "NEW"), help="rename a column (the Break)")
    g.add_argument("--rows", action="store_true", help="print the stored rows")
    a = p.parse_args(argv)
    if a.init:
        print(f"created {create_db(a.db)}")
    elif a.rename:
        rename_column(a.db, *a.rename)
        print(f"renamed {a.rename[0]!r} -> {a.rename[1]!r}")
    else:
        print(json.dumps(rows(a.db), indent=1))


if __name__ == "__main__":
    main()
