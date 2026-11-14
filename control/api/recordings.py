"""Recording endpoints. Delegate to `capture.session` and `matcher.match` (Capture workstream),
imported lazily: if they are missing the endpoints answer 501 with a clear message.

The id `fixture` (fixtures/recording.json) always works, with or without capture, so the
scripted BUILD flow can be driven from the UI before the recorder lands.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from control.api import sse
from runner.store import workspace_dir

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ID = "fixture"
FIXTURE_PATH = REPO_ROOT / "fixtures" / "recording.json"

router = APIRouter(prefix="/api/recordings", tags=["recordings"])


class StartRecording(BaseModel):
    intent: str
    allowlist: list[str] = Field(default_factory=list)
    start_url: str | None = None


def capture_session():
    try:
        return importlib.import_module("capture.session")
    except ImportError as e:
        raise HTTPException(501, f"Recording is not available in this build: capture.session could not be "
                                 f"imported ({e}). Use recording id 'fixture' instead.") from None


def matcher_match():
    try:
        return importlib.import_module("matcher").match
    except (ImportError, AttributeError) as e:
        raise HTTPException(501, f"The matcher is not available in this build: matcher.match could not be "
                                 f"imported ({e}).") from None


def recording_dir(recording_id: str) -> Path:
    return workspace_dir() / "recordings" / recording_id


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text())


def _summary(rec: dict[str, Any], recording_id: str, status: str = "stopped") -> dict[str, Any]:
    return {"id": recording_id, "intent": rec.get("intent"), "allowlist": rec.get("allowlist", []),
            "status": status, "started_at": rec.get("started_at"), "stopped_at": rec.get("stopped_at"),
            "n_events": len(rec.get("events", []))}


def _candidates(recording_id: str) -> dict[str, Any] | None:
    path = recording_dir(recording_id) / "candidates.json"
    return json.loads(path.read_text()) if path.exists() else None


def _session(recording_id: str):
    s = capture_session().get_session(recording_id)
    if s is None:
        raise HTTPException(404, f"recording {recording_id} not found")
    return s


def _entry(recording_id: str) -> dict[str, Any]:
    """The list_recordings() row: from the session's own summary() when it has one."""
    s = capture_session().get_session(recording_id)
    if s is not None and hasattr(s, "summary"):
        return s.summary()
    for e in capture_session().list_recordings():
        if e.get("id") == recording_id:
            return e
    return {"id": recording_id}


def load_recording(recording_id: str) -> dict[str, Any]:
    """The final recording.json (fixture or captured). 404 if unknown, 409 if still recording."""
    if recording_id == FIXTURE_ID:
        return _fixture()
    path = recording_dir(recording_id) / "recording.json"
    if path.exists():
        return json.loads(path.read_text())
    if recording_dir(recording_id).exists():
        raise HTTPException(409, f"recording {recording_id} is still running; stop it first")
    raise HTTPException(404, f"recording {recording_id} not found")


@router.post("")
def start(body: StartRecording) -> dict[str, Any]:
    headless = os.environ.get("PATCHWORK_CAPTURE_HEADLESS", "").lower() in ("1", "true", "yes")
    try:
        s = capture_session().start_recording(body.intent, body.allowlist, start_url=body.start_url,
                                              headless=headless)
    except ValueError as e:  # e.g. empty allowlist
        raise HTTPException(422, str(e)) from None
    return {"id": s.id}


@router.get("")
def list_all() -> list[dict[str, Any]]:
    out = [{**_summary(_fixture(), FIXTURE_ID), "fixture": True}] if FIXTURE_PATH.exists() else []
    try:
        cap = importlib.import_module("capture.session")
    except ImportError:
        return out
    return out + list(cap.list_recordings())


@router.post("/{recording_id}/stop")
def stop(recording_id: str) -> dict[str, Any]:
    # sync def: FastAPI runs it in the threadpool, since session.stop() blocks up to 20 s
    if recording_id == FIXTURE_ID:
        return _summary(_fixture(), FIXTURE_ID)
    rec = _session(recording_id).stop()
    return {**_summary(rec, recording_id), **{k: v for k, v in _entry(recording_id).items() if v is not None},
            "status": "stopped", "n_events": len(rec.get("events", []))}


@router.get("/{recording_id}")
def get(recording_id: str) -> dict[str, Any]:
    if recording_id == FIXTURE_ID:
        rec = _fixture()
        return {**_summary(rec, FIXTURE_ID), "events": rec["events"], "candidates": _candidates(FIXTURE_ID)}
    s = _session(recording_id)
    entry = _entry(recording_id)
    events = s.events()
    final = s.dir / "recording.json"
    if not events and final.exists():
        events = json.loads(final.read_text()).get("events", [])
    return {"id": recording_id, "intent": entry.get("intent"), "allowlist": entry.get("allowlist", []),
            "status": "recording" if s.running else "stopped", "started_at": entry.get("started_at"),
            "stopped_at": entry.get("stopped_at"), "n_events": len(events), "events": events,
            "candidates": _candidates(recording_id)}


@router.get("/{recording_id}/stream")
async def stream(recording_id: str, request: Request, after: int | None = None):
    """SSE: one `capture` event per captured event (id = 1-based index), then `stopped`."""
    start_at = sse.resume_from(request, after)

    if recording_id == FIXTURE_ID:
        rec = _fixture()

        async def fixture_gen():
            for i, ev in enumerate(rec["events"][start_at:], start=start_at + 1):
                yield sse.sse("capture", ev, i)
            yield sse.sse("stopped", _summary(rec, FIXTURE_ID), len(rec["events"]) + 1)
        return sse.stream(fixture_gen())

    s = _session(recording_id)

    async def gen():
        seq, idle = start_at, 0.0
        while not await request.is_disconnected():
            running = s.running
            evs = s.events(after=seq)
            for ev in evs:
                seq += 1
                yield sse.sse("capture", ev, seq)
            if not evs and not running:
                yield sse.sse("stopped", {**_entry(recording_id), "status": "stopped"}, seq + 1)
                return
            if evs:
                idle = 0.0
            else:
                idle += sse.POLL_S
                if idle >= sse.KEEPALIVE_S:
                    idle = 0.0
                    yield sse.keepalive()
            await asyncio.sleep(sse.POLL_S)
    return sse.stream(gen())


@router.post("/{recording_id}/candidates")
def candidates(recording_id: str) -> dict[str, Any]:
    rec = load_recording(recording_id)
    match = matcher_match()
    out = match(rec)
    d = recording_dir(recording_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "candidates.json").write_text(json.dumps(out, indent=2))
    return out
