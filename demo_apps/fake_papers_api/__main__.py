"""python -m demo_apps.fake_papers_api [--port 18030] [--host 127.0.0.1]"""

from __future__ import annotations

import argparse

import uvicorn

from demo_apps.fake_papers_api.app import create_app

RESERVED = {8000, 8001, 8002, 8003, 5180, 38091}  # the owner's live demo and stage


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="demo_apps.fake_papers_api")
    p.add_argument("--port", type=int, default=18030)
    p.add_argument("--host", default="127.0.0.1")
    args = p.parse_args(argv)
    if args.port in RESERVED:
        raise SystemExit(f"port {args.port} is reserved for the live demo; use 18030")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
