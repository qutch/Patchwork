"""python -m demo_apps.fake_airtable [--port 18020] [--host 127.0.0.1] [--token TOKEN]

Then: PATCHWORK_AIRTABLE_API_URL=http://127.0.0.1:18020/v0, base appPatchworkDemo1, table Applications."""

from __future__ import annotations

import argparse

import uvicorn

from demo_apps.fake_airtable.app import REAL_BASE_FIELDS, TOKEN, create_app

RESERVED = {8000, 8001, 8002, 8003, 5180, 38091}  # the owner's live demo and stage


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="demo_apps.fake_airtable")
    p.add_argument("--port", type=int, default=18020)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--token", default=TOKEN, help="the bearer token it accepts (default: the fixture token)")
    p.add_argument("--no-email", action="store_true", help="seed the table without Email, like the real demo base")
    args = p.parse_args(argv)
    if args.port in RESERVED:
        raise SystemExit(f"port {args.port} is reserved for the live demo; use 18020")
    uvicorn.run(create_app(token=args.token, fields=REAL_BASE_FIELDS if args.no_email else None), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
