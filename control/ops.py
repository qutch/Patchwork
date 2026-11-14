"""Control-plane operations. The CLI calls these today; the FastAPI service and the agents'
tools (validate_spec, compile_spec, run_tests, dry_run, request_deploy) wrap them next."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

from compiler.compile import CompileResult, compile_spec, load_examples
from runner.store import Store, workspace_dir
from spec import load_spec

REPO_ROOT = Path(__file__).resolve().parent.parent
SECRET_ENV = re.compile(r"(API_KEY|_TOKEN|SECRET|ANTHROPIC|OPENAI|LANGCHAIN|LANGSMITH)", re.I)
RUN_TIMEOUT_S = 300


def runner_env() -> dict[str, str]:
    """Stripped environment for the runner: no model keys, no secrets, explicit paths."""
    keep = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
    keep["PYTHONPATH"] = str(REPO_ROOT)
    keep["PATCHWORK_WORKSPACE"] = str(workspace_dir())
    return keep


def fetch_sink_openapi(base_url: str) -> dict[str, Any]:
    base = os.environ.get("PATCHWORK_SINK_BASE_URL") or base_url
    resp = httpx.get(base.rstrip("/") + "/openapi.json", timeout=10)
    resp.raise_for_status()
    return resp.json()


def compile_and_test(spec_path: str | Path, *, examples_path: str | Path | None = None,
                     transforms_path: str | Path | None = None, openapi_path: str | Path | None = None,
                     store: Store | None = None) -> dict[str, Any]:
    """validate -> compile vN -> register -> run generated tests. Never deploys."""
    store = store or Store()
    spec_path = Path(spec_path)
    examples_path = Path(examples_path) if examples_path else spec_path.with_name("examples.yaml")
    transforms_path = Path(transforms_path) if transforms_path else spec_path.with_name("transforms.py")
    spec = load_spec(spec_path)
    sink_openapi = (json.loads(Path(openapi_path).read_text()) if openapi_path
                    else fetch_sink_openapi(spec.sink.base_url))
    version = store.latest_version(spec.name) + 1
    result: CompileResult = compile_spec(
        spec, version=version, out_root=workspace_dir() / "integrations",
        examples=load_examples(examples_path),
        transforms_src=transforms_path.read_text() if transforms_path.exists() else None,
        sink_openapi=sink_openapi,
    )
    store.add_version(spec.name, version, result.spec_hash, result.openapi_hash)
    passed, summary = run_tests(spec.name, version, store=store)
    return {"name": spec.name, "version": version, "path": str(result.path), "tests_passed": passed,
            "tests_summary": summary, "contract_errors": result.contract_errors}


def run_tests(name: str, version: int, *, store: Store | None = None,
              sink_openapi_path: str | None = None) -> tuple[bool, str]:
    store = store or Store()
    vdir = workspace_dir() / "integrations" / name / f"v{version}"
    env = runner_env()
    if sink_openapi_path:
        env["PATCHWORK_SINK_OPENAPI"] = sink_openapi_path
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(vdir / "tests"), "-q", "-p", "no:cacheprovider",
         "--rootdir", str(vdir), "-c", os.devnull],
        capture_output=True, text=True, env=env, cwd=REPO_ROOT, timeout=RUN_TIMEOUT_S,
    )
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    summary = lines[-1] if lines else proc.stderr.strip()[-500:]
    passed = proc.returncode == 0
    if not passed:
        summary = "\n".join(lines[-40:]) or proc.stderr[-2000:]
    store.set_tests(name, version, passed, summary)
    return passed, summary


class DeployRefused(Exception):
    pass


def deploy(name: str, version: int, *, store: Store | None = None) -> dict[str, Any]:
    """Move the active pointer. Refuses unless the full suite passed on this exact version."""
    store = store or Store()
    v = store.version(name, version)
    if v is None:
        raise DeployRefused(f"{name} v{version} does not exist")
    if not v["tests_passed"]:
        raise DeployRefused(f"{name} v{version}: tests have not passed on this version")
    previous = store.active_version(name)
    store.set_active(name, version)
    return {"name": name, "active_version": version, "previous_version": previous}


def run_sandboxed(name: str, *, version: str = "active", dry_run: bool = False, limit: int | None = None,
                  timeout: int = RUN_TIMEOUT_S) -> dict[str, Any]:
    """Launch `python -m runner run` as a subprocess with a timeout, a scratch working
    directory and a stripped environment. Light sandboxing, labelled honestly."""
    cmd = [sys.executable, "-m", "runner", "run", name, "--version", str(version)]
    if dry_run:
        cmd.append("--dry-run")
    if limit is not None:
        cmd += ["--limit", str(limit)]
    with tempfile.TemporaryDirectory(prefix="patchwork-run-") as scratch:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=runner_env(), cwd=scratch,
                              timeout=timeout)
    summary: dict[str, Any] = {}
    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            summary = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    if not summary:
        summary = {"status": "failed", "message": (proc.stderr or proc.stdout)[-2000:]}
    summary["exit_code"] = proc.returncode
    return summary


def status(name: str, *, store: Store | None = None) -> dict[str, Any]:
    store = store or Store()
    return {
        "name": name,
        "active_version": store.active_version(name),
        "versions": [{k: v[k] for k in ("version", "tests_passed", "tests_summary", "created_at", "deployed_at")}
                     for v in store.versions(name)],
        "recent_runs": [{k: r[k] for k in ("id", "version", "dry_run", "status", "counts", "duration_s",
                                           "llm_calls", "incident_id")} for r in store.runs(name, limit=5)],
        "open_incidents": store.incidents(name),
    }
