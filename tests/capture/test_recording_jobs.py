"""fixtures/recording_jobs/ is what scripts/make_recording_jobs.py produces (W2-3)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIX = REPO / "fixtures" / "recording_jobs"


def _stable(mappings: list[dict]) -> list[dict]:
    # "Date Applied" is the day the recording was made; everything else must match exactly.
    return [{k: v for k, v in m.items() if not (m["dest_field"] == "Date Applied" and k == "value")}
            for m in mappings]


def test_fixture_recording_is_reproducible(tmp_path):
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        import make_recording_jobs
    finally:
        sys.path.pop(0)
    fresh = make_recording_jobs.make(tmp_path / "out")
    committed = json.loads((FIX / "candidates.json").read_text())
    assert _stable(fresh["mappings"]) == _stable(committed["mappings"])
    assert fresh["sink"] == committed["sink"] and fresh["source"] == committed["source"]
    assert fresh["recording_id"] == committed["recording_id"] == "fixture-jobs"
    assert (tmp_path / "out" / "snapshots" / "1.html").exists()
    rec = json.loads((FIX / "recording.json").read_text())
    assert rec["id"] == "fixture-jobs" and [e["type"] for e in rec["events"]].count("copy") == 2
