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
from runner import credentials
from runner.credentials import CRED_PREFIX, CredentialError
from runner.store import Store, workspace_dir
from spec import load_spec

REPO_ROOT = Path(__file__).resolve().parent.parent
SECRET_ENV = re.compile(r"(API_KEY|_TOKEN|SECRET|ANTHROPIC|OPENAI|LANGCHAIN|LANGSMITH)", re.I)
RUN_TIMEOUT_S = 300

# The control plane's own base URL (contracts V7: `patchwork://control`). The API sets it from the
# address it is serving on; an explicit PATCHWORK_CONTROL_URL in the environment wins.
CONTROL_URL: str | None = None


def credential_names(spec: Any) -> list[str]:
    """The credential names a run of `spec` needs (contracts V13): every `auth` block's `credential`
    (`source.auth`, `sink.auth`, any sink kind). `spec` is a spec model, a spec dict or a config dict."""
    return credentials.credential_names(spec)


def runner_env(spec: Any = None) -> dict[str, str]:
    """Stripped environment for the runner: no model keys, no secrets, explicit paths, and
    PATCHWORK_CONTROL_URL so `patchwork://control` sources resolve.

    V13: no credential reaches a runner by its plain name (nor an inherited PATCHWORK_CRED__*); then, for each
    name in `credential_names(spec)` only, PATCHWORK_CRED__<NAME>=<value from .env>. Raises CredentialError
    (message names the credential, never a value) when one is not set or names a model key. Without a spec
    (generated tests, v1/v2 integrations) no credential is added."""
    from control.credentials import known_names

    names = credential_names(spec)
    plain = {*known_names(), *names}
    keep = {k: v for k, v in os.environ.items()
            if not SECRET_ENV.search(k) and not k.startswith(CRED_PREFIX) and k not in plain}
    keep["PYTHONPATH"] = str(REPO_ROOT)
    keep["PATCHWORK_WORKSPACE"] = str(workspace_dir())
    if not keep.get("PATCHWORK_CONTROL_URL") and CONTROL_URL:
        keep["PATCHWORK_CONTROL_URL"] = CONTROL_URL
    for name in names:
        try:
            credentials.check_name(name)
        except ValueError as e:
            raise CredentialError(str(e)) from None
        keep[CRED_PREFIX + name] = credentials.get(name)
    return keep


def run_spec(name: str, version: str | int = "active", *, store: Store | None = None) -> dict[str, Any] | None:
    """The spec (as a dict) of the version a run of `name` would use, for `runner_env(spec)`; None when
    there is no such version (the runner then reports that itself)."""
    import yaml

    try:
        v = (store or Store()).active_version(name) if str(version) == "active" else int(version)
    except ValueError:
        return None
    if v is None:
        return None
    path = workspace_dir() / "integrations" / name / f"v{v}" / "spec.yaml"
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def sink_auth_headers(spec: Any) -> dict[str, str]:
    """Headers for the control plane's own requests to `spec`'s sink (contract fetches): {} without auth."""
    sink = spec.get("sink") if isinstance(spec, dict) else getattr(spec, "sink", None)
    auth = sink.get("auth") if isinstance(sink, dict) else getattr(sink, "auth", None)
    return credentials.headers(auth)


def fetch_sink_openapi(base_url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    base = os.environ.get("PATCHWORK_SINK_BASE_URL") or base_url
    resp = httpx.get(base.rstrip("/") + "/openapi.json", timeout=10, headers=headers or None)
    resp.raise_for_status()
    return resp.json()


def fetch_contract(spec: Any) -> dict[str, Any]:
    """The destination's current contract (contracts V6). API sinks: GET {base}/openapi.json (as
    `fetch_sink_openapi`). Form sinks: a headless snapshot of the form as an OpenAPI-shaped doc.
    PATCHWORK_SINK_BASE_URL never applies to form sinks. API sinks with `auth` send its headers (V13); the
    document returned never includes them."""
    if isinstance(spec, dict):
        if (spec.get("sink") or {}).get("kind") == "form":
            from runner.forms import fetch_form_contract

            return fetch_form_contract(spec["sink"]["url"])
    elif getattr(spec.sink, "kind", "api") == "form":
        from runner.forms import fetch_form_contract

        return fetch_form_contract(spec.sink.url)
    sink = spec["sink"] if isinstance(spec, dict) else spec.sink
    get = (lambda k: sink.get(k)) if isinstance(sink, dict) else (lambda k: getattr(sink, k, None))
    if get("kind") == "airtable":  # contracts V14: the table's writable fields, read with the sink's credential
        auth = get("auth")
        credential = auth.get("credential") if isinstance(auth, dict) else getattr(auth, "credential", None)
        return fetch_airtable_contract(get("base_id"), get("table"), credential, headers=sink_auth_headers(spec))
    if get("kind") == "sqlite":  # contracts V16: the table's columns, read from the file under the workspace
        return fetch_sqlite_contract(get("path"), get("table"))
    return fetch_sink_openapi(get("base_url"), headers=sink_auth_headers(spec))


def fetch_sqlite_contract(path: str, table: str) -> dict[str, Any]:
    """The SQLite table's V16 contract (PRAGMA table_xinfo, read-only). Errors read like the Airtable ones."""
    from runner import sqlite_sink
    from runner.lib import Quarantine

    try:
        return sqlite_sink.fetch_contract(path, table)
    except Quarantine as q:
        raise RuntimeError(f"{q.code}: {q.detail}") from None


def fetch_airtable_contract(base_id: str, table: str, credential: str,
                            headers: dict[str, str] | None = None) -> dict[str, Any]:
    """The Airtable table's V14 contract (meta API), read with the named credential from this process's
    environment (.env), or with `headers` (`sink_auth_headers(spec)`). Errors name the credential, never its value."""
    from runner import airtable
    from runner.lib import Quarantine

    try:
        return airtable.fetch_contract(base_id, table, headers or credentials.headers({"credential": credential}),
                                       credential=credential)
    except Quarantine as q:
        raise RuntimeError(f"{q.code}: {credentials.redact(q.detail, names=[credential])}") from None


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
                    else fetch_contract(spec))
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
    env = runner_env()  # never a spec: generated tests get no credentials (they use runner.testing.FakeHttp)
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


def deploy(name: str, version: int, *, store: Store | None = None, resolve_incidents: bool = True) -> dict[str, Any]:
    """Move the active pointer. Refuses unless the full suite passed on this exact version.
    Deploying a newer version resolves the open incidents raised on older versions."""
    store = store or Store()
    v = store.version(name, version)
    if v is None:
        raise DeployRefused(f"{name} v{version} does not exist")
    if not v["tests_passed"]:
        raise DeployRefused(f"{name} v{version}: tests have not passed on this version")
    previous = store.active_version(name)
    store.set_active(name, version)
    from control import drafts

    drafts.mark_live(name)  # a wizard draft becomes live once a version is deployed
    resolved = store.resolve_incidents(name, version) if resolve_incidents else []
    return {"name": name, "active_version": version, "previous_version": previous, "resolved_incidents": resolved}


def rollback(name: str, version: int, *, store: Store | None = None) -> dict[str, Any]:
    """Point the integration back at an earlier version (same guard as deploy: its tests passed).
    Only the pointer moves; versions are immutable and incidents are left as they are."""
    store = store or Store()
    active = store.active_version(name)
    if active is not None and version >= active:
        raise DeployRefused(f"{name}: rollback target v{version} is not older than active v{active}")
    out = deploy(name, version, store=store, resolve_incidents=False)
    return {**out, "rolled_back": True}


def runner_command(name: str, *, version: str | int = "active", dry_run: bool = False,
                   limit: int | None = None, replay: bool = False) -> list[str]:
    cmd = [sys.executable, "-m", "runner", "run", name, "--version", str(version)]
    if dry_run:
        cmd.append("--dry-run")
    if limit is not None:
        cmd += ["--limit", str(limit)]
    if replay:
        cmd.append("--replay-quarantined")
    return cmd


def parse_summary(stdout: str) -> dict[str, Any]:
    """The runner's last JSON stdout line that is not an event line ({"event": "started", ...})."""
    for line in reversed(stdout.strip().splitlines()):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "event" not in obj:
            return obj
    return {}


def run_sandboxed(name: str, *, version: str = "active", dry_run: bool = False, limit: int | None = None,
                  replay: bool = False, timeout: int = RUN_TIMEOUT_S) -> dict[str, Any]:
    """Launch `python -m runner run` as a subprocess with a timeout, a scratch working
    directory and a stripped environment. Light sandboxing, labelled honestly. Blocks until done;
    the API's background runs use the same command (control/api/runs.py)."""
    cmd = runner_command(name, version=version, dry_run=dry_run, limit=limit, replay=replay)
    spec = run_spec(name, version)
    try:
        env = runner_env(spec)
    except CredentialError as e:  # V13: the run refuses to start; the message names the credential only
        return {"status": "failed", "integration": name, "message": str(e), "llm_calls": 0, "run_id": None,
                "exit_code": None}
    with tempfile.TemporaryDirectory(prefix="patchwork-run-") as scratch:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=scratch,
                              timeout=timeout)
    summary = parse_summary(proc.stdout)
    if not summary:
        from control.credentials import redact

        summary = {"status": "failed", "message": redact((proc.stderr or proc.stdout)[-2000:], spec)}
    summary["exit_code"] = proc.returncode
    return summary


def replay_quarantined(name: str, *, store: Store | None = None) -> dict[str, Any]:
    """Re-run, through the active version, the raw records whose latest live outcome is
    quarantined. Records come from the DB (runner --replay-quarantined); idempotency keys make it
    safe. Returns the run summary."""
    store = store or Store()
    if store.active_version(name) is None:
        raise DeployRefused(f"{name}: no active version to replay on")
    return run_sandboxed(name, replay=True)


# ---- versions, diffs and listings (control-plane API) -------------------------------------

def version_dir(name: str, version: int) -> Path:
    return workspace_dir() / "integrations" / name / f"v{version}"


def _text_files(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts or p.suffix in (".pyc", ".db"):
            continue
        try:
            out[p.relative_to(root).as_posix()] = p.read_text()
        except UnicodeDecodeError:
            continue
    return out


def version_detail(name: str, version: int, *, store: Store | None = None) -> dict[str, Any] | None:
    store = store or Store()
    v = store.version(name, version)
    vdir = version_dir(name, version)
    if v is None or not vdir.is_dir():
        return None
    files = _text_files(vdir)
    return {"name": name, "version": version, "active": store.active_version(name) == version,
            "manifest": json.loads(files.get("manifest.json", "{}")), "spec_yaml": files.get("spec.yaml", ""),
            "files": files, "tests_passed": bool(v["tests_passed"]), "tests_summary": v["tests_summary"],
            "created_at": v["created_at"], "deployed_at": v["deployed_at"]}


def diff_versions(name: str, a: int, b: int) -> dict[str, Any]:
    """JSON Patch between the two specs (a -> b), a unified spec diff, and unified diffs of every
    generated file that differs. Raises FileNotFoundError if either version is missing."""
    import difflib

    import jsonpatch
    import yaml

    da, db = version_dir(name, a), version_dir(name, b)
    for d in (da, db):
        if not (d / "spec.yaml").exists():
            raise FileNotFoundError(f"{name} {d.name} does not exist")
    fa, fb = _text_files(da), _text_files(db)

    def udiff(x: str, y: str, rel: str) -> str:
        return "".join(difflib.unified_diff(x.splitlines(True), y.splitlines(True), f"v{a}/{rel}", f"v{b}/{rel}"))

    spec_a, spec_b = yaml.safe_load(fa["spec.yaml"]), yaml.safe_load(fb["spec.yaml"])
    code_diff = {}
    for rel in sorted(set(fa) | set(fb)):
        if rel == "spec.yaml" or fa.get(rel) == fb.get(rel):
            continue
        code_diff[rel] = udiff(fa.get(rel, ""), fb.get(rel, ""), rel)
    return {"name": name, "from": a, "to": b,
            "spec_patch": jsonpatch.make_patch(spec_a, spec_b).patch,
            "spec_diff": udiff(fa["spec.yaml"], fb["spec.yaml"], "spec.yaml"),
            "code_diff": code_diff}


def list_integrations(*, store: Store | None = None) -> list[dict[str, Any]]:
    store = store or Store()
    return [{"name": i["name"], "active_version": i["active_version"],
             "latest_version": store.latest_version(i["name"]),
             "open_incidents": len(store.incidents(i["name"], status="open"))}
            for i in store.integrations()]


def reset_workspace() -> list[str]:
    """Delete workspace state for a fresh demo: DB, run logs, integrations, agent threads.
    Recordings and settings are kept. Returns what was removed."""
    import shutil

    ws = workspace_dir()
    removed = []
    for sub in ("runs", "integrations", "agent_threads"):
        if (ws / sub).exists():
            shutil.rmtree(ws / sub, ignore_errors=True)
            removed.append(sub)
    for f in ws.glob("patchwork.db*"):
        f.unlink(missing_ok=True)
        removed.append(f.name)
    return removed


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
