"""python -m demo_apps.tracker_form [--port 18010] [--host 127.0.0.1]"""

from __future__ import annotations

import argparse

import uvicorn

from demo_apps.tracker_form.app import create_app


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="demo_apps.tracker_form")
    p.add_argument("--port", type=int, default=18010)
    p.add_argument("--host", default="127.0.0.1")
    args = p.parse_args(argv)
    if 8000 <= args.port <= 8003 or args.port == 5180:
        raise SystemExit(f"port {args.port} is reserved for the live demo; use 18010")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
