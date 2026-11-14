"""capture.session (what the control plane calls) and the `python -m capture record` CLI,
both running the real recorder subprocess with headless Chromium."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from capture import session as session_mod
from capture.session import get_session, list_recordings, start_recording
from tests.capture.helpers import netloc

REPO = Path(__file__).resolve().parents[2]


def _wait_for(pred, timeout=30.0, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def _has_source_get(events):
    return any(e["type"] == "net" and e["url"] == "/api/messages" for e in events)


def test_session_lifecycle(apps, workspace):
    allow = [netloc(apps["inbox"]), netloc(apps["live"])]
    t0 = time.time()
    s = start_recording("For every new PO, record incoming stock", allow, start_url=apps["inbox"] + "/",
                        headless=True)
    assert time.time() - t0 < 2, "start_recording must not block on the browser"
    assert re.fullmatch(r"rec-\d{8}-\d{6}-[0-9a-f]{4}", s.id)
    assert s.dir == workspace / "recordings" / s.id
    assert s.running

    listed = list_recordings()
    assert [r["id"] for r in listed] == [s.id] and listed[0]["status"] == "recording"

    events = _wait_for(lambda: _has_source_get(s.events()) and s.events(), what="the inbox GET")
    assert events[0]["type"] == "framenavigated"
    snapshot = s.events()
    assert s.events(after=1)[: len(snapshot) - 1] == snapshot[1:]  # 1-based index = line number

    # stop() from another thread (the API calls it from a worker thread)
    result: dict = {}
    th = threading.Thread(target=lambda: result.update(rec=s.stop()))
    th.start()
    th.join(timeout=30)
    rec = result["rec"]
    assert not s.running
    assert rec["id"] == s.id and rec["intent"].startswith("For every new PO") and rec["allowlist"] == allow
    assert rec["stopped_at"] >= rec["started_at"] and _has_source_get(rec["events"])
    assert json.loads((s.dir / "recording.json").read_text()) == rec
    assert (s.dir / "candidates.json").exists()
    assert s.stop() == rec  # idempotent

    summary = list_recordings()[0]
    assert summary["status"] == "stopped" and summary["n_events"] == len(rec["events"])
    assert set(summary) == {"id", "intent", "allowlist", "status", "started_at", "stopped_at", "n_events"}

    # A fresh process (empty registry) still finds the finished recording on disk
    session_mod._sessions.clear()
    again = get_session(s.id)
    assert again is not None and not again.running and again.stop() == rec
    assert len(again.events()) == len(rec["events"])
    assert get_session("rec-does-not-exist") is None


def test_fixture_session(workspace):
    s = get_session("fixture")
    assert s is not None and not s.running
    rec = s.stop()
    assert rec["id"] == "fixture-po-001" and s.events() == rec["events"]
    assert s.events(after=len(rec["events"]) - 1) == rec["events"][-1:]


def test_start_requires_allowlist(workspace):
    with pytest.raises(ValueError):
        start_recording("x", [])


def test_cli_sigterm_writes_recording(apps, workspace):
    out = workspace / "recordings" / "rec-cli"
    env = dict(os.environ, PYTHONPATH=str(REPO))
    proc = subprocess.Popen(
        [sys.executable, "-m", "capture", "record", str(out), "--intent", "cli test",
         "--allow", netloc(apps["inbox"]), "--start-url", apps["inbox"] + "/", "--headless"],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        _wait_for(lambda: (out / "events.jsonl").exists() and
                  _has_source_get([json.loads(ln) for ln in (out / "events.jsonl").read_text().splitlines()]),
                  what="live events.jsonl")
        assert not (out / "recording.json").exists()
        proc.send_signal(signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, stderr
    assert '"event": "stopped"' in stdout
    rec = json.loads((out / "recording.json").read_text())
    assert rec["id"] == "rec-cli" and rec["intent"] == "cli test" and _has_source_get(rec["events"])
    assert json.loads((out / "meta.json").read_text())["status"] == "stopped"
