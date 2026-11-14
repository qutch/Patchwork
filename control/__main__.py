"""Patchwork control CLI (the MVP stand-in for the control plane API and UI).

  python -m control validate fixtures/spec.yaml
  python -m control compile  fixtures/spec.yaml       # compile vN + run generated tests
  python -m control deploy   po_to_stockroom 1         # human approval moves the pointer
  python -m control run      po_to_stockroom [--dry-run] [--limit N]
  python -m control status   po_to_stockroom
  python -m control incident 1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from control import ops
from runner.store import Store
from spec import validate_spec


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="control")
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="validate a spec file")
    v.add_argument("spec")

    c = sub.add_parser("compile", help="compile the next version and run its generated tests")
    c.add_argument("spec")
    c.add_argument("--examples")
    c.add_argument("--transforms")
    c.add_argument("--openapi", help="use this OpenAPI file instead of fetching the sink's")

    d = sub.add_parser("deploy", help="approve a version and make it active")
    d.add_argument("name")
    d.add_argument("version", type=int)
    d.add_argument("--yes", action="store_true", help="skip the interactive approval prompt")

    r = sub.add_parser("run", help="run an integration in the sandboxed runner (no LLM)")
    r.add_argument("name")
    r.add_argument("--version", default="active")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--limit", type=int)

    s = sub.add_parser("status", help="versions, recent runs and open incidents")
    s.add_argument("name")

    i = sub.add_parser("incident", help="show one incident")
    i.add_argument("id", type=int)

    args = p.parse_args(argv)

    if args.cmd == "validate":
        errors = validate_spec(Path(args.spec).read_text())
        _print({"valid": not errors, "errors": errors})
        return 0 if not errors else 1

    if args.cmd == "compile":
        out = ops.compile_and_test(args.spec, examples_path=args.examples, transforms_path=args.transforms,
                                   openapi_path=args.openapi)
        _print(out)
        return 0 if out["tests_passed"] and not out["contract_errors"] else 1

    if args.cmd == "deploy":
        store = Store()
        ver = store.version(args.name, args.version)
        if ver is None:
            print(f"{args.name} v{args.version} does not exist", file=sys.stderr)
            return 1
        print(f"Deploy {args.name} v{args.version}? tests: {ver['tests_summary']!r}; "
              f"currently active: v{store.active_version(args.name)}")
        if not args.yes and input("Approve [y/N]: ").strip().lower() != "y":
            print("not approved")
            return 1
        try:
            _print(ops.deploy(args.name, args.version, store=store))
        except ops.DeployRefused as e:
            print(f"refused: {e}", file=sys.stderr)
            return 1
        return 0

    if args.cmd == "run":
        out = ops.run_sandboxed(args.name, version=args.version, dry_run=args.dry_run, limit=args.limit)
        _print(out)
        return out.get("exit_code", 1)

    if args.cmd == "status":
        _print(ops.status(args.name))
        return 0

    if args.cmd == "incident":
        inc = Store().incident(args.id)
        if inc is None:
            print("no such incident", file=sys.stderr)
            return 1
        for k in ("old_openapi", "new_openapi"):
            if inc.get(k):
                inc[k] = f"<{len(json.dumps(inc[k]))} bytes>"
        _print(inc)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
