"""In-process v2 stack for the benchmark and the offline smoke: fixture tracker form (V8) and the control plane
(V9) with the scripted agent, each a uvicorn server in a thread on a free port (never 8000-8003 / 5180), with its
own PATCHWORK_WORKSPACE. Talks to everything over HTTP, like the extension and the UI do.

Needs W1 (demo_apps.tracker_form, form sinks), W3 (start_learn) and W4a (/api/pages) merged; `missing()` says
which parts are not there yet, so callers can report it instead of failing obscurely.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any

from bench.stack import Server

NAME = "jobs_to_tracker"


def missing() -> list[str]:
    """Integration pieces this stack needs that aren't importable in this checkout."""
    out = []
    for mod, attr, who in (("demo_apps.tracker_form.app", "create_app", "W1 fixture tracker form"),
                           ("runner.forms", "fill_and_submit", "W1 form sink runtime"),
                           ("control.pages", "PageInbox", "W4a page inbox"),
                           ("agents.scripted", "ScriptedAgentService", "agents")):
        try:
            m = importlib.import_module(mod)
        except ImportError as e:
            out.append(f"{who}: cannot import {mod} ({e})")
            continue
        if not hasattr(m, attr):
            out.append(f"{who}: {mod}.{attr} missing")
    try:
        from agents.scripted import ScriptedAgentService
        if not hasattr(ScriptedAgentService, "start_learn"):
            out.append("W3 scripted LEARN: ScriptedAgentService.start_learn missing")
    except ImportError:
        pass
    return out


class V2Stack:
    """`with V2Stack(workspace) as st:` -> st.form, st.control URLs; env restored on exit."""

    def __init__(self, workspace: Path, *, learn_policy: str = "auto"):
        self.workspace = Path(workspace)
        self.learn_policy = learn_policy
        self._servers: list[Server] = []
        self._old_env: dict[str, str | None] = {}
        self.form = self.control = ""
        self.agent: Any = None

    def _setenv(self, **kw: str) -> None:
        for k, v in kw.items():
            self._old_env.setdefault(k, os.environ.get(k))
            os.environ[k] = v

    def _start(self, srv: Server) -> str:
        srv.__enter__()
        self._servers.append(srv)
        return srv.url

    def __enter__(self) -> V2Stack:
        gaps = missing()
        if gaps:
            raise RuntimeError("v2 stack needs integration: " + "; ".join(gaps))
        from demo_apps.tracker_form.app import create_app as form_app

        self.workspace.mkdir(parents=True, exist_ok=True)
        self._setenv(PATCHWORK_WORKSPACE=str(self.workspace), PATCHWORK_NO_DOTENV="1", PATCHWORK_RUN_PACE_MS="0",
                     PATCHWORK_AI_ENABLED="1", PATCHWORK_AGENT="scripted")
        self.form = self._start(Server(form_app()))
        self._setenv(PATCHWORK_TRACKER_FORM_URL=self.form + "/")

        from agents.scripted import ScriptedAgentService
        from control.api import create_app

        self.agent = ScriptedAgentService(pace_s=0)
        srv = Server(create_app(agent=self.agent, dotenv=False))
        # V7: runners resolve patchwork://control to PATCHWORK_CONTROL_URL; the port is known before start
        self._setenv(PATCHWORK_CONTROL_URL=srv.url)
        self.control = self._start(srv)
        return self

    def __exit__(self, *exc: object) -> None:
        for srv in reversed(self._servers):
            srv.__exit__(*exc)
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
