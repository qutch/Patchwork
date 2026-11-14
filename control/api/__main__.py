"""python -m control.api [--port 38080] [--host 127.0.0.1]"""

from __future__ import annotations

import argparse

import uvicorn


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="control.api")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=38080)
    p.add_argument("--log-level", default="info")
    args = p.parse_args(argv)
    uvicorn.run("control.api:app", host=args.host, port=args.port, log_level=args.log_level,
                timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
