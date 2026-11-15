"""`./patchwork` (scripts/patchwork.py, one-command launcher design): port choice and the UI rebuild check as
units with fakes, and one real background start -> status -> stop on an injected port range (47100-47120), far
from the reserved ports (8000-8003, 5180) and the owner's stage (38091), which this file never touches."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from scripts import patchwork as pw

REPO = Path(__file__).resolve().parents[1]
RANGE = range(47100, 47121)
WS = Path("/tmp/pw-test-ws")


def _choose(preferred=47100, fallback=range(47101, 47121), *, taken=(), patchwork=None, exact=False, ws=WS):
    """choose_port with fakes: `taken` ports are busy; `patchwork` maps a busy port to the workspace it serves."""
    patchwork = patchwork or {}
    return pw.choose_port(preferred, fallback, ws, is_free=lambda p: p not in taken and p not in patchwork,
                          patchwork_workspace=lambda p: patchwork.get(p), exact=exact)


# ---- port choice ---------------------------------------------------------------------------------

def test_preferred_port_when_free():
    assert _choose() == pw.PortChoice(47100, False)


def test_preferred_port_taken_by_patchwork_on_this_workspace_is_reused():
    assert _choose(patchwork={47100: str(WS)}) == pw.PortChoice(47100, True)


def test_preferred_port_taken_by_something_else_gives_the_next_free():
    assert _choose(taken={47100, 47101}) == pw.PortChoice(47102, False)
    # another workspace's Patchwork is "something else": never reuse a different workspace
    assert _choose(patchwork={47100: "/elsewhere"}) == pw.PortChoice(47101, False)


def test_reserved_ports_are_never_chosen():
    reserved = [8000, 8001, 8002, 8003, 5180]
    assert _choose(preferred=8000, fallback=[*reserved, 47105]) == pw.PortChoice(47105, False)
    assert pw.RESERVED_PORTS == frozenset(reserved)
    preferred, fallback = pw.port_plan({})
    assert preferred == 38091 and list(fallback) == list(range(38092, 38111))
    assert not pw.RESERVED_PORTS & {preferred, *fallback}
    with pytest.raises(pw.LauncherError, match="kept for the other demo apps"):
        _choose(preferred=5180, fallback=(), exact=True)


def test_exact_port_taken_is_a_clear_error():
    assert _choose(preferred=47110, fallback=(), exact=True) == pw.PortChoice(47110, False)
    with pytest.raises(pw.LauncherError, match=r"port 47110 is taken by another program.*--port"):
        _choose(preferred=47110, fallback=(), taken={47110}, exact=True)
    with pytest.raises(pw.LauncherError, match=r"another Patchwork \(workspace /elsewhere\)"):
        _choose(preferred=47110, fallback=(), patchwork={47110: "/elsewhere"}, exact=True)
    assert _choose(preferred=47110, fallback=(), patchwork={47110: str(WS)}, exact=True) == pw.PortChoice(47110, True)


def test_no_free_port_is_a_clear_error():
    with pytest.raises(pw.LauncherError, match="no free port in 47100-47102"):
        _choose(fallback=[47101, 47102], taken={47100, 47101, 47102})


def test_port_range_from_the_environment():
    assert pw.port_plan({"PATCHWORK_PORT_RANGE": "47100-47120"}) == (47100, range(47101, 47121))
    for bad in ("47100", "a-b", "47120-47100", "80-90"):
        with pytest.raises(pw.LauncherError):
            pw.port_plan({"PATCHWORK_PORT_RANGE": bad})


# ---- the UI rebuild check ------------------------------------------------------------------------

def _touch(path: Path, t: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("x")
    os.utime(path, (t, t))


def test_ui_rebuild_check_from_mtimes(tmp_path):
    web = tmp_path / "web"
    now = time.time()
    _touch(web / "src" / "pages" / "Home.tsx", now - 100)
    _touch(web / "index.html", now - 100)
    _touch(web / "package.json", now - 100)
    assert pw.ui_is_stale(web)  # never built
    _touch(web / "dist" / "index.html", now - 50)
    assert not pw.ui_is_stale(web)  # fresh
    _touch(web / "src" / "pages" / "Home.tsx", now - 10)
    assert pw.ui_is_stale(web)  # a source changed after the build
    _touch(web / "dist" / "index.html", now)
    assert not pw.ui_is_stale(web)
    _touch(web / "package.json", now + 10)
    assert pw.ui_is_stale(web)
    _touch(web / "dist" / "index.html", now + 20)
    _touch(web / "node_modules" / "x" / "index.js", now + 30)  # not an input
    _touch(web / "dist" / "assets" / "x.js", now + 30)  # nor is the build itself
    assert not pw.ui_is_stale(web)


# ---- lifecycle: the one real process ---------------------------------------------------------------

def _run(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run([str(REPO / "patchwork"), *args], cwd=REPO, env=env, capture_output=True, text=True,
                          timeout=90)


def test_background_start_status_stop(tmp_path):
    ws = tmp_path / "ws"
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>Patchwork UI</title>")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PATCHWORK_")}
    env.update(PATCHWORK_PORT_RANGE=f"{RANGE[0]}-{RANGE[-1]}", PATCHWORK_NO_DOTENV="1", PATCHWORK_AGENT="scripted",
               PATCHWORK_WEB_DIST=str(dist))
    common = ["--workspace", str(ws)]
    pid = None
    started = _run("--background", "--no-open", "--no-build", *common, env=env)
    try:
        assert started.returncode == 0, started.stdout + started.stderr
        url = (ws / "patchwork.url").read_text().strip()
        port = int(url.rsplit(":", 1)[1])
        assert url.startswith("http://127.0.0.1:") and port in RANGE
        assert f"Patchwork is running at {url}" in started.stdout
        pid = int((ws / "patchwork.pid").read_text())
        assert pw.pid_alive(pid)
        assert (ws / "patchwork.log").exists()

        h = pw.health(url)
        assert h and Path(h["workspace"]).resolve() == ws.resolve()
        assert "Patchwork UI" in pw._NO_PROXY.open(url + "/integrations/x", timeout=5).read().decode()

        again = _run("--background", "--no-open", "--no-build", *common, env=env)  # same workspace: reused
        assert again.returncode == 0 and "already running" in again.stdout
        assert int((ws / "patchwork.pid").read_text()) == pid

        status = _run("status", *common, env=env)
        assert status.returncode == 0 and url in status.stdout and str(ws) in status.stdout
        assert "AI         on (scripted agent)" in status.stdout
        pw._NO_PROXY.open(url + "/api/extension", timeout=5).read()  # the token now exists
        token = (ws / "extension_token").read_text().strip()
        status = _run("status", *common, env=env)
        for out in (started.stdout, started.stderr, status.stdout, status.stderr, (ws / "patchwork.log").read_text()):
            assert token not in out  # the extension token is never printed or logged
    finally:
        stopped = _run("stop", *common, env=env)
    assert stopped.returncode == 0 and "stopped" in stopped.stdout
    assert pid is not None
    deadline = time.time() + 5
    while pw.pid_alive(pid) and time.time() < deadline:
        time.sleep(0.1)
    assert not pw.pid_alive(pid)
    assert not (ws / "patchwork.pid").exists() and not (ws / "patchwork.url").exists()
    assert _run("status", *common, env=env).returncode == 1
    assert _run("stop", *common, env=env).returncode == 0  # stopping twice is fine


def test_no_ai_saves_the_switch_off(tmp_path):
    (tmp_path / "settings.json").write_text('{"ai_enabled": true, "learn_policy": "auto"}')
    pw.turn_ai_off(tmp_path)
    assert pw.json.loads((tmp_path / "settings.json").read_text()) == {"ai_enabled": False, "learn_policy": "auto"}
