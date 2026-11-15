"""`./patchwork`: start Patchwork with one command. One process serves the API and the built UI on one port.

Design: docs/superpowers/specs/2026-11-14-one-command-launcher-design.md.

    ./patchwork                 start in the foreground, open the browser; Ctrl-C stops it
    ./patchwork --background    start detached (log: <workspace>/patchwork.log); ./patchwork stop stops it
    ./patchwork status          running or not, the address, the workspace, AI on or off
    ./patchwork stop            stop the copy recorded in <workspace>/patchwork.pid
    --fresh | --workspace PATH  a new temporary workspace | that workspace (default: workspace-v2/)
    --port N                    exactly this port (else 38091, then the first free one in 38092-38110)
    --no-open | --no-ai | --no-build

Ports 8000-8003 and 5180 are never chosen (the other demo apps use them). `PATCHWORK_PORT_RANGE=A-B` replaces
38091-38110 (tests use a range far from the reserved ports). The extension finds Patchwork by probing
38091-38110, so a port outside that range needs the extension's Advanced address.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

REPO = Path(__file__).resolve().parents[1]
WEB = REPO / "web"
HOST = "127.0.0.1"
PREFERRED_PORT = 38091
FALLBACK_PORTS = range(38092, 38111)
RESERVED_PORTS = frozenset({8000, 8001, 8002, 8003, 5180})
DEFAULT_WORKSPACE = REPO / "workspace-v2"
PID_FILE, URL_FILE, LOG_FILE = "patchwork.pid", "patchwork.url", "patchwork.log"
START_TIMEOUT_S = 60.0
STOP_TIMEOUT_S = 10.0
UI_INPUTS = ("src", "index.html", "package.json", "vite.config.ts")


class LauncherError(Exception):
    """A clear message for the person; printed without a stack trace (exit 2)."""


# ---- ports --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class PortChoice:
    port: int
    reuse: bool  # True: Patchwork on this workspace is already serving it


def port_plan(env: dict[str, str] | None = None) -> tuple[int, range]:
    """(preferred, fallback): 38091 then 38092-38110, or `PATCHWORK_PORT_RANGE=A-B` (A preferred, A+1..B after)."""
    raw = (env if env is not None else os.environ).get("PATCHWORK_PORT_RANGE", "").strip()
    if not raw:
        return PREFERRED_PORT, FALLBACK_PORTS
    try:
        lo, hi = (int(x) for x in raw.split("-", 1))
    except ValueError:
        raise LauncherError(f"PATCHWORK_PORT_RANGE must look like 47100-47120, not {raw!r}") from None
    if not 1024 <= lo <= hi <= 65535:
        raise LauncherError(f"PATCHWORK_PORT_RANGE {raw!r} is not a range of ports 1024-65535")
    return lo, range(lo + 1, hi + 1)


def same_path(a: str | Path | None, b: str | Path) -> bool:
    if not a:
        return False
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def choose_port(preferred: int, fallback: Iterable[int], workspace: Path, *,
                is_free: Callable[[int], bool], patchwork_workspace: Callable[[int], str | None],
                exact: bool = False, reserved: frozenset[int] = RESERVED_PORTS) -> PortChoice:
    """The port to serve on. A port in use by Patchwork on this same workspace is reused (nothing new starts);
    one used by anything else (another workspace's Patchwork included) is skipped, or with `exact` is an error.
    Reserved ports are never chosen. `patchwork_workspace(port)` is the workspace a Patchwork there reports,
    or None when nothing (or not Patchwork) answers."""
    if exact:
        if preferred in reserved:
            raise LauncherError(f"port {preferred} is kept for the other demo apps (8000-8003, 5180); choose another")
        if is_free(preferred):
            return PortChoice(preferred, False)
        other = patchwork_workspace(preferred)
        if same_path(other, workspace):
            return PortChoice(preferred, True)
        who = f"another Patchwork (workspace {other})" if other else "another program"
        raise LauncherError(f"port {preferred} is taken by {who}. Stop it, or leave out --port to pick a free one.")
    for port in [preferred, *fallback]:
        if port in reserved:
            continue
        if is_free(port):
            return PortChoice(port, False)
        if same_path(patchwork_workspace(port), workspace):
            return PortChoice(port, True)
    ports = [preferred, *fallback]
    raise LauncherError(f"no free port in {ports[0]}-{ports[-1]}; stop something there, or pass --port N")


def is_free(port: int, host: str = HOST) -> bool:
    """Nothing answers on host:port, and we could listen there."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        if s.connect_ex((host, port)) == 0:
            return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, port))
        return True
    except OSError:
        return False


_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def health(url: str, timeout: float = 1.0) -> dict | None:
    """GET {url}/api/health, if the answer is Patchwork's (`status: ok` and a `workspace`); else None."""
    body = get_json(f"{url}/api/health", timeout)
    if body is not None and body.get("status") == "ok" and "workspace" in body:
        return body
    return None


def patchwork_workspace(port: int) -> str | None:
    h = health(f"http://{HOST}:{port}")
    return h["workspace"] if h else None


# ---- the UI build -------------------------------------------------------------------------------

def newest_mtime(paths: Iterable[Path]) -> float:
    newest = 0.0
    for p in paths:
        if p.is_dir():
            for f in p.rglob("*"):
                if f.is_file():
                    newest = max(newest, f.stat().st_mtime)
        elif p.is_file():
            newest = max(newest, p.stat().st_mtime)
    return newest


def ui_is_stale(web: Path = WEB) -> bool:
    """True when web/dist/index.html is missing or older than anything under web/src, web/index.html,
    web/package.json or web/vite.config.ts."""
    index = web / "dist" / "index.html"
    if not index.is_file():
        return True
    return newest_mtime(web / name for name in UI_INPUTS) > index.stat().st_mtime


def build_ui(web: Path = WEB, say: Callable[[str], None] = print) -> None:
    """`npm run build` in web/ (`npm ci` first when node_modules is missing). If it cannot build but an older
    build exists, that build is served and the person is told; with no build at all it is an error."""
    has_old = (web / "dist" / "index.html").is_file()
    npm = shutil.which("npm")
    if npm is None:
        msg = "Node.js (npm) is not installed, so the UI cannot be built. Install Node.js 20+ (https://nodejs.org" \
              " or `brew install node`), then run ./patchwork again."
        if has_old:
            say(f"warning: {msg} Serving the previous build for now.")
            return
        raise LauncherError(msg)
    steps = []
    if not (web / "node_modules").is_dir():
        steps.append(("Installing the UI's packages (npm ci, first run only)...", [npm, "ci", "--no-audit", "--no-fund"]))
    steps.append(("Building the UI (web/)...", [npm, "run", "-s", "build"]))
    for label, cmd in steps:
        say(label)
        done = subprocess.run(cmd, cwd=web, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if done.returncode != 0:
            tail = "\n".join(done.stdout.strip().splitlines()[-15:])
            msg = f"`{' '.join(cmd[1:])}` in web/ failed:\n{tail}"
            if has_old:
                say(f"warning: {msg}\nServing the previous build for now.")
                return
            raise LauncherError(msg)


# ---- the workspace's pid and url files ----------------------------------------------------------

def read_pid(ws: Path) -> int | None:
    try:
        return int((ws / PID_FILE).read_text().strip())
    except (OSError, ValueError):
        return None


def read_url(ws: Path) -> str | None:
    try:
        return (ws / URL_FILE).read_text().strip() or None
    except OSError:
        return None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def is_our_server(pid: int) -> bool:
    """The pid is alive and is a control plane (not an unrelated process that reused the number)."""
    if not pid_alive(pid):
        return False
    try:
        cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
    except OSError:
        return True
    return "control.api" in cmd


def clear_files(ws: Path, pid: int | None = None) -> None:
    """Remove the pid and url files (only if they still name `pid`, when given)."""
    if pid is not None and read_pid(ws) not in (None, pid):
        return
    for name in (PID_FILE, URL_FILE):
        try:
            (ws / name).unlink()
        except OSError:
            pass


def running(ws: Path) -> tuple[int, str] | None:
    """(pid, url) of the Patchwork this workspace's files name, if it is alive and answering."""
    pid, url = read_pid(ws), read_url(ws)
    if pid is None or url is None or not is_our_server(pid):
        return None
    h = health(url)
    return (pid, url) if h and same_path(h["workspace"], ws) else None


# ---- commands -----------------------------------------------------------------------------------

def resolve_workspace(args: argparse.Namespace) -> Path:
    if getattr(args, "fresh", False):
        return Path(tempfile.mkdtemp(prefix="patchwork-")).resolve()
    if args.workspace:
        return Path(args.workspace).expanduser().resolve()
    return DEFAULT_WORKSPACE


def get_json(url: str, timeout: float = 1.0) -> dict | None:
    try:
        with _NO_PROXY.open(url, timeout=timeout) as r:
            body = json.loads(r.read().decode())
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def describe_ai(url: str) -> str:
    """"on (deep agent, <model>)" from GET /api/settings (the model name, never a key)."""
    s = get_json(f"{url}/api/settings")
    if s is None:
        return "unknown"
    agent, model = s.get("agent", ""), s.get("model", "")
    detail = f"{agent} agent" + (f", {model}" if model and model != "scripted" else "")
    return ("on" if s.get("ai_enabled") else "off") + f" ({detail})"


def status_block(url: str, ws: Path, background: bool) -> str:
    """The one short block a start or `status` prints. Never the extension token."""
    where = "" if same_path(ws, DEFAULT_WORKSPACE) else f" --workspace {ws}"
    extension = "finds it by itself" if _in_extension_range(url) else \
        f"set {url} in its options (Advanced); it finds 38091-38110 by itself"
    lines = [f"Patchwork is running at {url}",
             f"  workspace  {ws}",
             f"  AI         {describe_ai(url)}",
             f"  extension  {extension}",
             f"  stop       {'./patchwork stop' + where if background else 'Ctrl-C'}"]
    return "\n".join(lines)


def _in_extension_range(url: str) -> bool:
    try:
        port = int(url.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return False
    return PREFERRED_PORT <= port <= FALLBACK_PORTS[-1]


def open_browser(url: str, args: argparse.Namespace) -> None:
    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - no browser is fine; the URL is printed
            pass


def server_env(ws: Path, args: argparse.Namespace) -> dict[str, str]:
    """The control plane's environment: `.env` (as the control plane loads it), the workspace, the deep agent,
    AI on unless --no-ai. Same as `make stage-v2`. A PATCHWORK_AGENT already set in the shell wins."""
    from control.api.settings import load_dotenv

    load_dotenv()
    env = dict(os.environ)
    env["PATCHWORK_WORKSPACE"] = str(ws)
    env.setdefault("PATCHWORK_AGENT", "deep")
    env["PATCHWORK_AI_ENABLED"] = "0" if args.no_ai else "1"
    env.pop("PATCHWORK_CONTROL_URL", None)  # the server learns its own address
    return env


def turn_ai_off(ws: Path) -> None:
    """--no-ai on a workspace whose settings already say AI on: the saved switch wins over the env default,
    so save it off (the UI's switch turns it back on)."""
    path = ws / "settings.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    data["ai_enabled"] = False
    path.write_text(json.dumps(data, indent=2))


def wait_healthy(proc: subprocess.Popen, url: str, ws: Path, timeout: float = START_TIMEOUT_S) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise LauncherError(f"the control plane exited (code {proc.returncode}) before it was ready")
        h = health(url, timeout=0.5)
        if h is not None:
            if not same_path(h["workspace"], ws):
                raise LauncherError(f"{url} answers as a Patchwork on another workspace ({h['workspace']})")
            return h
        time.sleep(0.15)
    raise LauncherError(f"the control plane did not answer at {url} within {timeout:.0f} s")


def stop_process(pid: int, timeout: float = STOP_TIMEOUT_S) -> bool:
    """SIGTERM, then SIGKILL after `timeout`. True when it is gone."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid) or _reaped(pid):
            return True
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    time.sleep(0.2)
    return not pid_alive(pid) or _reaped(pid)


def _reaped(pid: int) -> bool:
    """Reap our own exited child (a zombie still answers kill(pid, 0))."""
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == pid
    except ChildProcessError:
        return False


def cmd_start(args: argparse.Namespace) -> int:
    ws = resolve_workspace(args)
    ws.mkdir(parents=True, exist_ok=True)
    say = print

    current = running(ws)
    if current is not None:
        say(f"Patchwork is already running at {current[1]} (workspace {ws}).")
        open_browser(current[1], args)
        return 0

    if args.port is not None:
        choice = choose_port(args.port, (), ws, is_free=is_free, patchwork_workspace=patchwork_workspace, exact=True)
    else:
        preferred, fallback = port_plan()
        choice = choose_port(preferred, fallback, ws, is_free=is_free, patchwork_workspace=patchwork_workspace)
    url = f"http://{HOST}:{choice.port}"
    if choice.reuse:
        say(f"Patchwork is already running at {url} (workspace {ws}).")
        open_browser(url, args)
        return 0

    if not args.no_build and ui_is_stale():
        build_ui(say=say)
    from control.api.ui import ui_dist
    if not (ui_dist() / "index.html").is_file():
        say(f"warning: there is no UI build ({ui_dist()}); only the API will answer. Run without --no-build.")

    env = server_env(ws, args)
    if args.no_ai:
        turn_ai_off(ws)
    cmd = [sys.executable, "-m", "control.api", "--host", HOST, "--port", str(choice.port)]
    if args.background:
        log = open(ws / LOG_FILE, "ab")
        proc = subprocess.Popen([*cmd, "--log-level", "info"], cwd=REPO, env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        log.close()
    else:
        proc = subprocess.Popen([*cmd, "--log-level", "warning"], cwd=REPO, env=env, stdin=subprocess.DEVNULL)
    (ws / PID_FILE).write_text(f"{proc.pid}\n")

    try:
        wait_healthy(proc, url, ws)
    except (LauncherError, KeyboardInterrupt) as e:
        stop_process(proc.pid)
        clear_files(ws, proc.pid)
        if isinstance(e, LauncherError) and args.background:
            raise LauncherError(f"{e}. See {ws / LOG_FILE}.") from None
        raise
    (ws / URL_FILE).write_text(f"{url}\n")
    say(status_block(url, ws, args.background))
    open_browser(url, args)
    if args.background:
        return 0
    return _foreground(proc, ws)


def _foreground(proc: subprocess.Popen, ws: Path) -> int:
    """Wait for the server. Ctrl-C (or SIGTERM/SIGHUP to this launcher) stops it cleanly, no stack trace."""
    def _stop(signum, frame):  # noqa: ARG001
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGHUP, _stop)
    try:
        code = proc.wait()
        clear_files(ws, proc.pid)
        if code not in (0, -signal.SIGTERM, -signal.SIGINT):
            print(f"Patchwork stopped (the control plane exited with code {code}).")
            return 1
        print("Patchwork stopped.")
        return 0
    except KeyboardInterrupt:
        signal.signal(signal.SIGINT, signal.SIG_IGN)  # a second Ctrl-C must not leave the server behind
        print("\nStopping Patchwork...")
        try:
            proc.wait(timeout=STOP_TIMEOUT_S)  # Ctrl-C reached the server too; give it its graceful shutdown
        except subprocess.TimeoutExpired:
            stop_process(proc.pid)
        clear_files(ws, proc.pid)
        print("Patchwork stopped.")
        return 0


def cmd_status(args: argparse.Namespace) -> int:
    ws = resolve_workspace(args)
    current = running(ws)
    if current is None:
        print(f"Patchwork is not running (workspace {ws}).")
        return 1
    pid, url = current
    print(status_block(url, ws, background=True))
    print(f"  pid        {pid}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    ws = resolve_workspace(args)
    pid = read_pid(ws)
    if pid is None or not is_our_server(pid):
        clear_files(ws)
        print(f"Patchwork is not running (workspace {ws}).")
        return 0
    if not stop_process(pid):
        raise LauncherError(f"could not stop process {pid}")
    clear_files(ws, pid)
    print(f"Patchwork stopped (workspace {ws}).")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="./patchwork", description="Start Patchwork: the API and the UI on one port.")
    p.add_argument("command", nargs="?", default="start", choices=["start", "stop", "status"])
    p.add_argument("--background", action="store_true", help="start detached; ./patchwork stop stops it")
    where = p.add_mutually_exclusive_group()
    where.add_argument("--workspace", metavar="PATH", help="workspace folder (default: workspace-v2/)")
    where.add_argument("--fresh", action="store_true", help="a new, empty temporary workspace (rehearsals)")
    p.add_argument("--port", type=int, metavar="N", help="exactly this port; fails if it is taken")
    p.add_argument("--no-open", action="store_true", help="don't open the browser")
    p.add_argument("--no-ai", action="store_true", help="start with AI off")
    p.add_argument("--no-build", action="store_true", help="serve web/dist as it is, without rebuilding the UI")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    try:
        if args.command == "stop":
            return cmd_stop(args)
        if args.command == "status":
            return cmd_status(args)
        return cmd_start(args)
    except LauncherError as e:
        print(f"patchwork: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
