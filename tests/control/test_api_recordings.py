"""Recording endpoints: 501 without capture, the built-in `fixture` id, and a fake capture session
(the real one comes from the Capture workstream)."""

import json
import sys
import threading
import time
import types
from pathlib import Path

import httpx
import pytest

from control.api import create_app
from tests.conftest import _Server
from tests.control.conftest import read_sse


class FakeSession:
    def __init__(self, id: str, dir: Path, intent: str, allowlist: list[str]):
        self.id, self.dir, self.intent, self.allowlist = id, dir, intent, allowlist
        self.dir.mkdir(parents=True, exist_ok=True)
        self._events: list[dict] = []
        self._running = True

    def add(self, n: int) -> None:
        for _ in range(n):
            self._events.append({"t": len(self._events), "type": "click", "selector": f"#b{len(self._events)}"})

    def events(self, after: int = 0) -> list[dict]:
        return self._events[after:]

    @property
    def running(self) -> bool:
        return self._running

    def stop(self) -> dict:
        self._running = False
        rec = {"id": self.id, "intent": self.intent, "allowlist": self.allowlist, "started_at": 1, "stopped_at": 2,
               "events": list(self._events)}
        (self.dir / "recording.json").write_text(json.dumps(rec))
        return rec


@pytest.fixture
def fake_capture(workspace, monkeypatch):
    sessions: dict[str, FakeSession] = {}
    mod = types.ModuleType("capture.session")

    def start_recording(intent, allowlist, start_url=None, headless=False):
        rid = f"rec-20261114-120000-{len(sessions):04x}"
        sessions[rid] = FakeSession(rid, workspace / "recordings" / rid, intent, allowlist)
        sessions[rid].start_url = start_url
        return sessions[rid]

    mod.start_recording = start_recording
    mod.get_session = sessions.get
    mod.list_recordings = lambda: [
        {"id": s.id, "intent": s.intent, "allowlist": s.allowlist, "status": "recording" if s.running else "stopped",
         "started_at": 1, "stopped_at": None if s.running else 2, "n_events": len(s._events)} for s in sessions.values()]
    matcher = types.ModuleType("matcher")
    matcher.match = lambda rec: {"recording_id": rec["id"], "intent": rec["intent"], "mappings": [],
                                 "n_events": len(rec["events"])}
    monkeypatch.setitem(sys.modules, "capture", types.ModuleType("capture"))
    monkeypatch.setitem(sys.modules, "capture.session", mod)
    monkeypatch.setitem(sys.modules, "matcher", matcher)
    return sessions


@pytest.fixture
def plain_api(workspace, monkeypatch, tmp_path):
    """The API without demo apps (recordings don't need them). The v2 `fixture-jobs` recording is
    hidden here (tests/control/test_api_v2_demo.py covers it), so these v1 assertions hold after W2 lands."""
    from control.api import recordings
    monkeypatch.setattr(recordings, "FIXTURE_JOBS_DIR", tmp_path / "no-recording-jobs")
    with _Server(create_app(dotenv=False)) as srv, httpx.Client(base_url=srv.url, timeout=30) as c:
        yield c


def test_without_capture_endpoints_answer_501(plain_api, monkeypatch):
    monkeypatch.setitem(sys.modules, "capture.session", None)  # import fails even once capture/ lands
    monkeypatch.setitem(sys.modules, "matcher", None)
    r = plain_api.post("/api/recordings", json={"intent": "x", "allowlist": ["localhost:8001"]})
    assert r.status_code == 501 and "capture.session" in r.json()["detail"]
    for method, url in [("POST", "/api/recordings/rec-1/stop"), ("GET", "/api/recordings/rec-1"),
                        ("GET", "/api/recordings/rec-1/stream")]:
        assert plain_api.request(method, url).status_code == 501
    # the fixture recording still works, and the list still shows it
    assert [r["id"] for r in plain_api.get("/api/recordings").json()] == ["fixture"]
    assert plain_api.post("/api/recordings/fixture/candidates").status_code == 501  # needs the matcher


def test_fixture_recording(plain_api, monkeypatch):
    monkeypatch.setitem(sys.modules, "capture.session", None)
    [fx] = plain_api.get("/api/recordings").json()
    assert fx["fixture"] and fx["status"] == "stopped" and fx["n_events"] == 12
    rec = plain_api.get("/api/recordings/fixture").json()
    assert rec["intent"] and len(rec["events"]) == 12 and rec["candidates"] is None
    evs = read_sse(plain_api, "/api/recordings/fixture/stream")
    assert [e["event"] for e in evs] == ["capture"] * 12 + ["stopped"]
    assert [e["id"] for e in evs[:-1]] == list(range(1, 13))
    evs = read_sse(plain_api, "/api/recordings/fixture/stream", headers={"Last-Event-ID": "10"})
    assert [e["id"] for e in evs] == [11, 12, 13]
    assert plain_api.post("/api/recordings/fixture/stop").json()["status"] == "stopped"


def test_recording_lifecycle_with_fake_capture(plain_api, fake_capture, workspace):
    r = plain_api.post("/api/recordings", json={"intent": "PO to stock", "allowlist": ["localhost:8001"],
                                                "start_url": "http://localhost:8001/"})
    assert r.status_code == 200
    rid = r.json()["id"]
    session = fake_capture[rid]
    assert session.start_url == "http://localhost:8001/"
    session.add(3)

    listed = {x["id"]: x for x in plain_api.get("/api/recordings").json()}
    assert listed[rid]["status"] == "recording" and "fixture" in listed
    got = plain_api.get(f"/api/recordings/{rid}").json()
    assert got["status"] == "recording" and len(got["events"]) == 3 and got["intent"] == "PO to stock"
    assert plain_api.post(f"/api/recordings/{rid}/candidates").status_code == 409  # not stopped yet
    assert plain_api.post("/api/integrations/build", json={"recording_id": rid}).status_code == 409

    # live stream: events arrive while recording, then "stopped"
    def later():
        time.sleep(0.4)
        session.add(2)
        time.sleep(0.4)
        session.stop()
    threading.Thread(target=later, daemon=True).start()
    evs = read_sse(plain_api, f"/api/recordings/{rid}/stream?after=1")
    assert [e["id"] for e in evs if e["event"] == "capture"] == [2, 3, 4, 5]
    assert evs[-1]["event"] == "stopped" and evs[-1]["data"]["status"] == "stopped"

    stopped = plain_api.post(f"/api/recordings/{rid}/stop").json()
    assert stopped["id"] == rid and stopped["status"] == "stopped" and stopped["n_events"] == 5
    cands = plain_api.post(f"/api/recordings/{rid}/candidates").json()
    assert cands == {"recording_id": rid, "intent": "PO to stock", "mappings": [], "n_events": 5}
    assert json.loads((workspace / "recordings" / rid / "candidates.json").read_text()) == cands
    assert plain_api.get(f"/api/recordings/{rid}").json()["candidates"] == cands

    assert plain_api.get("/api/recordings/rec-unknown").status_code == 404
    assert plain_api.post("/api/recordings/rec-unknown/stop").status_code == 404
    assert plain_api.post("/api/recordings/rec-unknown/candidates").status_code == 404


def test_fixture_candidates_with_matcher(plain_api, fake_capture, workspace):
    cands = plain_api.post("/api/recordings/fixture/candidates").json()
    assert cands["recording_id"] == "fixture-po-001"
    assert plain_api.get("/api/recordings/fixture").json()["candidates"] == cands


def test_real_capture_session_through_the_api(api, apps, workspace, monkeypatch):
    """The real recorder (headless Chromium) driven only through the API: start -> live stream ->
    stop -> get -> candidates (real matcher)."""
    pytest.importorskip("capture.session")
    pytest.importorskip("playwright")
    monkeypatch.setenv("PATCHWORK_CAPTURE_HEADLESS", "1")

    def netloc(url: str) -> str:
        return url.split("://", 1)[1]

    assert api.post("/api/recordings", json={"intent": "x", "allowlist": []}).status_code == 422
    r = api.post("/api/recordings", json={"intent": "For every new PO, record incoming stock",
                                          "allowlist": [netloc(apps["inbox"]), netloc(apps["live"])],
                                          "start_url": apps["inbox"] + "/"})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    first = read_sse(api, f"/api/recordings/{rid}/stream", until=lambda e: e["event"] == "capture", timeout=60)
    assert first[0]["id"] == 1 and first[0]["data"]["type"] in ("framenavigated", "net")
    listed = {x["id"]: x for x in api.get("/api/recordings").json()}
    assert listed[rid]["status"] == "recording"

    stopped = api.post(f"/api/recordings/{rid}/stop").json()
    assert stopped["status"] == "stopped" and stopped["n_events"] >= 1
    evs = read_sse(api, f"/api/recordings/{rid}/stream")
    assert evs[-1]["event"] == "stopped" and [e["id"] for e in evs[:-1]] == list(range(1, len(evs)))
    assert any(e["data"].get("type") == "framenavigated" for e in evs[:-1])
    got = api.get(f"/api/recordings/{rid}").json()
    assert got["status"] == "stopped" and got["intent"].startswith("For every") and got["events"]
    cands = api.post(f"/api/recordings/{rid}/candidates").json()
    assert cands["recording_id"] == rid and "mappings" in cands

    fx = api.post("/api/recordings/fixture/candidates").json()  # real matcher on the fixture
    assert fx["sink"]["path"] == "/api/incoming" and fx["mappings"]
