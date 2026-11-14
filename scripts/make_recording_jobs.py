"""Regenerate fixtures/recording_jobs/ (recording id `fixture-jobs`, docs/contracts.md V11).

Drives the real recorder headless over fixtures/pages/lever-1.html (served at its real Lever URL by a Playwright
route, offline) and the fixture tracker form (a free port, reached as http://127.0.0.1:18010): copy the title, copy
the company, paste both, type demo@example.com, the link and today's date, submit. Writes recording.json,
candidates.json and snapshots/1.html.

    .venv/bin/python scripts/make_recording_jobs.py [--out fixtures/recording_jobs] [--date 2026-11-14]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests.capture.form_app import create_app  # noqa: E402
from tests.capture.jobs_demo import record_jobs_demo  # noqa: E402
from tests.conftest import _Server  # noqa: E402

RECORDING_ID = "fixture-jobs"


def make(out: Path, today: str | None = None) -> dict:
    """Record into a temp dir, then copy the fixture files to `out`. Returns the candidates."""
    with tempfile.TemporaryDirectory() as tmp, _Server(create_app()) as form:
        rec_dir = Path(tmp) / RECORDING_ID
        asyncio.run(record_jobs_demo(rec_dir, form.url, recording_id=RECORDING_ID, today=today))
        out.mkdir(parents=True, exist_ok=True)
        if (out / "snapshots").exists():
            shutil.rmtree(out / "snapshots")
        shutil.copytree(rec_dir / "snapshots", out / "snapshots")
        for name in ("recording.json", "candidates.json"):
            shutil.copy(rec_dir / name, out / name)
    return json.loads((out / "candidates.json").read_text())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, default=REPO / "fixtures" / "recording_jobs")
    p.add_argument("--date", help="the 'Date Applied' typed into the form (default: today)")
    args = p.parse_args(argv)
    cands = make(args.out, args.date)
    print(json.dumps({"out": str(args.out), "mappings": cands["mappings"], "sink": cands["sink"]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
