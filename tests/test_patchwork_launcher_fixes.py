"""`./patchwork` review fixes (round 1): quick restarts keep their port (TIME_WAIT), `stop` only stops our own
server, `--no-ai` lasts one run, `--fresh` is refused for stop/status. Ports come from the injected range
47100-47120 only; 38091 and the reserved ports are never touched."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts import patchwork as pw
from tests.test_patchwork_launcher import RANGE, WS, _run


def _bindable_port() -> int:
    """A port in the injected range we can listen on right now."""
    for port in RANGE:
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    pytest.skip("no free port in 47100-47120")


def _leave_server_time_wait(port: int) -> None:
    """Listen on `port`, accept one connection and close it from the server side first, so the server's socket is
    left in TIME_WAIT, as after every `./patchwork stop`."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    cli = socket.create_connection(("127.0.0.1", port))
    conn, _ = srv.accept()
    conn.close()  # server side first -> TIME_WAIT on 127.0.0.1:port
    time.sleep(0.05)
    cli.close()
    srv.close()


def test_a_port_in_time_wait_is_free_and_chosen_again():
    port = _bindable_port()
    _leave_server_time_wait(port)
    assert pw.is_free(port)
    choice = pw.choose_port(port, range(port + 1, RANGE[-1] + 1), WS, is_free=pw.is_free,
                            patchwork_workspace=pw.patchwork_workspace)
    assert choice == pw.PortChoice(port, False)
    _leave_server_time_wait(port)
    assert pw.choose_port(port, (), WS, is_free=pw.is_free, patchwork_workspace=pw.patchwork_workspace,
                          exact=True) == pw.PortChoice(port, False)


def test_a_listening_port_is_still_taken():
    port = _bindable_port()
    with socket.socket() as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        assert not pw.is_free(port)


def test_stop_never_kills_a_process_that_is_not_ours(tmp_path):
    """A stale pid file whose pid now belongs to another process (even another control plane) is left alone."""
    ws = tmp_path / "ws"
    ws.mkdir()
    sleep = "import time; time.sleep(60)"
    others = [subprocess.Popen([sys.executable, "-c", sleep]),
              # looks like a control plane, but on another port
              subprocess.Popen([sys.executable, "-c", sleep, "-m", "control.api", "--port", "47199"])]
    try:
        for other in others:
            (ws / "patchwork.pid").write_text(f"{other.pid}\n")
            (ws / "patchwork.url").write_text("http://127.0.0.1:47198\n")
            assert not pw.is_our_server(other.pid, 47198)
            done = _run("stop", "--workspace", str(ws), env={**os.environ, "PATCHWORK_NO_DOTENV": "1"})
            assert done.returncode == 0 and "not running" in done.stdout, done.stdout + done.stderr
            time.sleep(0.2)
            assert other.poll() is None, "an unrelated process was killed"
        assert pw.is_our_server(others[1].pid, 47199)  # the port in its command line is what makes it ours
    finally:
        for other in others:
            other.kill()
            other.wait()


def test_ps_failure_is_not_ours(monkeypatch):
    def boom(*a, **k):
        raise OSError("no ps")
    monkeypatch.setattr(pw.subprocess, "run", boom)
    assert not pw.is_our_server(os.getpid(), 47100)


def test_fresh_is_refused_for_stop_and_status():
    for cmd in ("stop", "status"):
        done = _run(cmd, "--fresh", env={**os.environ, "PATCHWORK_NO_DOTENV": "1"})
        assert done.returncode == 2 and "--fresh" in done.stderr, done.stderr


def _env_for(dist: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PATCHWORK_")}
    env.update(PATCHWORK_PORT_RANGE=f"{RANGE[0]}-{RANGE[-1]}", PATCHWORK_NO_DOTENV="1", PATCHWORK_AGENT="scripted",
               PATCHWORK_WEB_DIST=str(dist))
    return env


@pytest.mark.parametrize("saved", [None, True])
def test_no_ai_lasts_this_run_only(tmp_path, saved):
    ws = tmp_path / "ws"
    ws.mkdir()
    if saved is not None:
        (ws / "settings.json").write_text(json.dumps({"ai_enabled": saved, "learn_policy": "approve"}))
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html>")
    env, common = _env_for(dist), ["--workspace", str(ws)]
    try:
        started = _run("--background", "--no-open", "--no-build", "--no-ai", *common, env=env)
        assert started.returncode == 0, started.stdout + started.stderr
        assert "AI         off (this run only" in started.stdout
        assert pw.health((ws / "patchwork.url").read_text().strip())["ai_enabled"] is False
        assert "AI         off (this run only" in _run("status", *common, env=env).stdout
    finally:
        assert _run("stop", *common, env=env).returncode == 0
    restored = json.loads((ws / "settings.json").read_text()) if (ws / "settings.json").exists() else {}
    assert restored.get("ai_enabled", True) is True
    if saved is not None:
        assert restored == {"ai_enabled": True, "learn_policy": "approve"}
    try:
        again = _run("--background", "--no-open", "--no-build", *common, env=env)
        assert again.returncode == 0, again.stdout + again.stderr
        assert "AI         on (scripted agent)" in again.stdout
    finally:
        _run("stop", *common, env=env)


def test_no_ai_left_over_by_a_crash_is_restored_on_the_next_start(tmp_path):
    (tmp_path / "settings.json").write_text('{"ai_enabled": false}')
    (tmp_path / "patchwork.ai-restore").write_text('{"ai_enabled": true}')
    pw.restore_ai(tmp_path)
    assert json.loads((tmp_path / "settings.json").read_text()) == {"ai_enabled": True}
    assert not (tmp_path / "patchwork.ai-restore").exists()
    (tmp_path / "settings.json").write_text('{"ai_enabled": false, "learn_policy": "auto"}')
    (tmp_path / "patchwork.ai-restore").write_text('{"ai_enabled": null}')  # nothing was saved before
    pw.restore_ai(tmp_path)
    assert json.loads((tmp_path / "settings.json").read_text()) == {"learn_policy": "auto"}
    pw.restore_ai(tmp_path)  # nothing to restore: no change
    assert json.loads((tmp_path / "settings.json").read_text()) == {"learn_policy": "auto"}
