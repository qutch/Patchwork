"""Contracts V13: destinations and API sources authenticate with a credential named by the spec and valued
in the environment (.env); no credential value ever reaches the run logs, records, incidents, agent tool
results or generated tests. Offline: a fake source+sink on a free port that checks the credentials and
echoes the Authorization header back in its error bodies (the worst case for redaction)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from control import ops
from runner import credentials
from runner.credentials import CredentialError
from runner.lib import AI_MODULES
from runner.store import Store
from spec import parse_spec
from spec.models import Auth
from tests.conftest import REPO, _Server

NAME = "cred_demo"
TOKEN = "tok-SENTINEL-123456"
KEY = "key-SENTINEL-654321"
SENTINELS = (TOKEN, KEY, "SENTINEL")


class Item(BaseModel):
    title: str
    n: int


def secure_app(token: str = TOKEN, key: str = KEY) -> FastAPI:
    """Source (GET /api/records, needs X-Api-Key) and sink (POST /api/items + /openapi.json, need a bearer
    token). Error bodies echo the headers they got, so a leak would show up in every detail."""
    app = FastAPI(openapi_url=None)
    app.state.records = [{"id": "r1", "kind": "a", "body": "title=foo n=3"},
                         {"id": "r2", "kind": "a", "body": "title=echo n=4"}]
    app.state.items, app.state.seen = [], []
    app.state.openapi_needs_auth = True

    def bad_auth(request: Request) -> JSONResponse | None:
        got = request.headers.get("authorization", "")
        app.state.seen.append(got)
        if got != f"Bearer {token}":
            return JSONResponse({"detail": f"bad token: {got!r}"}, status_code=401)
        return None

    @app.get("/openapi.json")
    def openapi(request: Request):
        if app.state.openapi_needs_auth and (err := bad_auth(request)) is not None:
            return err
        from fastapi.openapi.utils import get_openapi

        sub = FastAPI()

        @sub.post("/api/items", status_code=201)
        def _create(item: Item) -> dict:
            return {}

        return get_openapi(title="Secure sink", version="1", routes=sub.routes)

    @app.get("/api/records")
    def records(request: Request):
        got = request.headers.get("x-api-key", "")
        if got != key:
            return JSONResponse({"detail": f"bad key: {got!r}"}, status_code=401)
        # a source that echoes the key into a record: the agents' tools must still never show it
        return [*([{"id": "r-echo", "kind": "a", "body": f"title=leak n=1 key={got}"}]
                  if getattr(app.state, "echo_key", False) else []), *app.state.records]

    @app.post("/api/items", status_code=201)
    async def create(request: Request):
        if (err := bad_auth(request)) is not None:
            return err
        body = await request.json()
        if body.get("title") == "echo":  # a 422 that quotes the request headers back
            return JSONResponse({"detail": f"rejected; you sent {request.headers.get('authorization')}"},
                                status_code=422)
        app.state.items.append(body)
        return {"ok": True}

    return app


def write_spec(root: Path, url: str, *, sink_auth: dict | None = None, source_auth: dict | None = None) -> Path:
    spec = {
        "spec_version": 1, "name": NAME, "intent": "copy records to items",
        "source": {"kind": "api", "base_url": url, "list": {"method": "GET", "path": "/api/records"},
                   "record_id": "id", "text_field": "body",
                   "auth": source_auth or {"scheme": "header", "header": "X-Api-Key", "credential": "SOURCE_KEY"}},
        "extract": {"route_by": "kind", "parsers": [
            {"id": "p1", "fields": {"title": {"regex": r"title=(\w+)"}, "n": {"regex": r"n=(\d+)", "cast": "int"}}}]},
        "dedupe": {"key": ["title"]},
        "sink": {"kind": "api", "base_url": url, "staging_base_url": url,
                 "call": {"method": "POST", "path": "/api/items"},
                 "body": {"title": {"from": "title"}, "n": {"from": "n"}},
                 "auth": sink_auth or {"scheme": "bearer", "credential": "SINK_TOKEN"}},
    }
    d = root / "draft"
    d.mkdir(exist_ok=True)
    (d / "spec.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))
    (d / "examples.yaml").write_text(yaml.safe_dump([
        {"name": "foo", "record": {"id": "r1", "kind": "a", "body": "title=foo n=3"},
         "expect": {"body": {"title": "foo", "n": 3}}}]))
    return d / "spec.yaml"


@pytest.fixture
def secure(workspace, monkeypatch):
    monkeypatch.setenv("SINK_TOKEN", TOKEN)
    monkeypatch.setenv("SOURCE_KEY", KEY)
    for var in ("PATCHWORK_SOURCE_BASE_URL", "PATCHWORK_SINK_BASE_URL", "PATCHWORK_STAGING_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    app = secure_app()
    with _Server(app) as srv:
        yield srv, app


def _everything_stored(log_path: str | None) -> str:
    """Every byte a run leaves behind: its JSONL log, the runs/records/incidents tables."""
    store = Store()
    dump = [Path(log_path).read_text() if log_path and Path(log_path).exists() else ""]
    for table in ("runs", "records", "incidents"):
        dump += [json.dumps(r, default=str) for r in store.db.execute(f"SELECT * FROM {table}").fetchall()]
    return "\n".join(map(str, dump))


def _no_sentinel(text: str) -> None:
    for s in SENTINELS:
        assert s not in text, f"credential value leaked: ...{text[max(0, text.find(s) - 120):text.find(s) + 40]}..."


# ---- spec ----------------------------------------------------------------------------------------

def test_spec_auth_names_a_credential_and_refuses_model_keys(tmp_path):
    assert Auth(credential="SINK_TOKEN").scheme == "bearer"
    for bad in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "PATCHWORK_CRED__X", "lower_case"):
        with pytest.raises(ValidationError):
            Auth(credential=bad)
    with pytest.raises(ValidationError):
        Auth(scheme="header", credential="STOCK_API_KEY")  # header scheme needs a header name
    spec = parse_spec(yaml.safe_load(write_spec(tmp_path, "http://x").read_text()))
    assert spec.sink.auth.credential == "SINK_TOKEN" and spec.source.auth.header == "X-Api-Key"
    assert ops.credential_names(spec) == ["SINK_TOKEN", "SOURCE_KEY"]
    assert ops.credential_names(yaml.safe_load((tmp_path / "draft" / "spec.yaml").read_text())) == \
        ["SINK_TOKEN", "SOURCE_KEY"]
    assert ops.credential_names(parse_spec(yaml.safe_load((REPO / "fixtures" / "spec.yaml").read_text()))) == []


def test_spec_without_auth_compiles_exactly_as_before(workspace):
    """v1/v2 packages: no `auth` key in spec.yaml, config.yaml or manifest.json."""
    from compiler.compile import compile_spec, load_examples
    from spec import load_spec

    res = compile_spec(load_spec(REPO / "fixtures" / "spec.yaml"), version=1, out_root=workspace / "integrations",
                       examples=load_examples(REPO / "fixtures" / "examples.yaml"))
    assert "auth" not in (res.path / "config.yaml").read_text()
    assert "auth" not in (res.path / "spec.yaml").read_text()
    assert "credentials" not in json.loads((res.path / "manifest.json").read_text())


# ---- runner_env -----------------------------------------------------------------------------------

def test_runner_env_adds_only_the_referenced_credentials(workspace, monkeypatch):
    monkeypatch.setenv("STOCK_PASS", "pass-SENTINEL-777777")
    monkeypatch.setenv("UNRELATED_PASS", "other-value-888888")
    monkeypatch.setenv("PATCHWORK_CRED__INHERITED", "inherited-999999")
    spec = {"sink": {"auth": {"scheme": "bearer", "credential": "STOCK_PASS"}}}
    env = ops.runner_env(spec)
    assert env["PATCHWORK_CRED__STOCK_PASS"] == "pass-SENTINEL-777777"
    assert "STOCK_PASS" not in env  # never by its plain name
    assert "PATCHWORK_CRED__INHERITED" not in env
    assert not any(k.startswith("PATCHWORK_CRED__") and k != "PATCHWORK_CRED__STOCK_PASS" for k in env)
    assert not any(k.startswith("PATCHWORK_CRED__") for k in ops.runner_env())  # no spec: no credentials


def test_runner_env_refuses_missing_credentials_and_model_keys(workspace, monkeypatch):
    monkeypatch.delenv("SINK_TOKEN", raising=False)
    with pytest.raises(CredentialError) as e:
        ops.runner_env({"sink": {"auth": {"credential": "SINK_TOKEN"}}})
    assert str(e.value).startswith("credential SINK_TOKEN is not set: add SINK_TOKEN=... to .env")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-SENTINEL-000000")
    with pytest.raises(CredentialError) as e:
        ops.runner_env({"sink": {"auth": {"credential": "OPENAI_API_KEY"}}})
    assert "model keys never reach the runner" in str(e.value) and "sk-SENTINEL" not in str(e.value)


# ---- end to end ------------------------------------------------------------------------------------

def test_api_sink_and_source_with_auth_end_to_end(secure, workspace, monkeypatch):
    srv, app = secure
    spec_path = write_spec(workspace, srv.url)
    built = ops.compile_and_test(spec_path)  # fetches the contract with the sink's bearer header
    assert built["tests_passed"], built["tests_summary"]
    assert f"Bearer {TOKEN}" in app.state.seen
    vdir = Path(built["path"])
    config = yaml.safe_load((vdir / "config.yaml").read_text())
    assert config["sink"]["auth"] == {"scheme": "bearer", "credential": "SINK_TOKEN"}
    assert config["source"]["auth"]["credential"] == "SOURCE_KEY"
    assert json.loads((vdir / "manifest.json").read_text())["credentials"] == ["SINK_TOKEN", "SOURCE_KEY"]
    for f in vdir.rglob("*"):
        if f.is_file() and f.suffix in (".py", ".yaml", ".json"):
            _no_sentinel(f.read_text())
    ops.deploy(NAME, 1)

    summary = ops.run_sandboxed(NAME)
    assert summary["status"] == "completed", summary
    assert summary["llm_calls"] == 0
    assert app.state.items == [{"title": "foo", "n": 3}]  # sent with the bearer token, source read with the key
    recs = {r["record_id"]: r for r in Store().records(summary["run_id"])}
    assert recs["r1"]["status"] == "success"
    assert recs["r2"]["reason"] == "SINK_CONTRACT" and "***" in recs["r2"]["detail"]  # the echo, redacted
    _no_sentinel(json.dumps(summary) + _everything_stored(summary["log"]))

    # a wrong token: preflight (the contract fetch) is refused, naming the credential only
    monkeypatch.setenv("SINK_TOKEN", "tok-WRONG-SENTINEL-999")
    app.state.records.append({"id": "r3", "kind": "a", "body": "title=bar n=5"})
    pre = ops.run_sandboxed(NAME)
    assert pre["status"] == "failed"
    assert pre["message"] == f"preflight: 127.0.0.1:{srv.port} rejected the credential SINK_TOKEN (HTTP 401)"
    _no_sentinel(json.dumps(pre) + _everything_stored(pre["log"]))

    # ... and with a public contract, every write: AUTH names the credential, never the value (nor the echo)
    app.state.openapi_needs_auth = False
    bad = ops.run_sandboxed(NAME)
    rec = {r["record_id"]: r for r in Store().records(bad["run_id"])}["r3"]
    assert rec["reason"] == "AUTH"
    assert rec["detail"] == f"127.0.0.1:{srv.port} rejected the credential SINK_TOKEN (HTTP 401)"
    _no_sentinel(json.dumps(bad) + _everything_stored(bad["log"]))

    # three echoes in a row trip the breaker: the incident's samples carry the details, redacted
    monkeypatch.setenv("SINK_TOKEN", TOKEN)
    app.state.records[:] = [{"id": f"e{i}", "kind": "a", "body": "title=echo n=1"} for i in range(3)]
    tripped = ops.run_sandboxed(NAME)
    assert tripped["status"] == "tripped" and tripped["incident_id"]
    incident = Store().incident(tripped["incident_id"])
    assert "***" in json.dumps(incident["samples"])
    _no_sentinel(json.dumps(incident, default=str) + _everything_stored(tripped["log"]))

    # missing credential: the run refuses to start, nothing is written, no value shown
    monkeypatch.delenv("SINK_TOKEN")
    before = len(Store().runs(NAME, limit=100))
    refused = ops.run_sandboxed(NAME)
    assert refused["status"] == "failed" and refused["run_id"] is None
    assert refused["message"].startswith("credential SINK_TOKEN is not set: add SINK_TOKEN=... to .env")
    assert len(Store().runs(NAME, limit=100)) == before


def test_runner_without_its_credential_fails_clearly(secure, workspace, monkeypatch):
    """A runner launched by hand (no PATCHWORK_CRED__ in its env): a failed run naming the credential."""
    srv, _ = secure
    built = ops.compile_and_test(write_spec(workspace, srv.url))
    ops.deploy(NAME, built["version"])
    env = ops.runner_env()  # no spec: no credentials
    proc = subprocess.run(ops.runner_command(NAME), capture_output=True, text=True, env=env, cwd=workspace,
                          timeout=60)
    summary = ops.parse_summary(proc.stdout)
    assert summary["status"] == "failed"
    assert "credential SINK_TOKEN is not set" in summary["message"] or "credential SOURCE_KEY is not set" in \
        summary["message"]


def test_run_tests_never_gets_credentials(secure, workspace, monkeypatch):
    srv, _ = secure
    built = ops.compile_and_test(write_spec(workspace, srv.url))
    seen: list[dict] = []
    real = subprocess.run

    def spy(cmd, *a, **kw):
        seen.append(kw.get("env") or {})
        return real(cmd, *a, **kw)

    monkeypatch.setattr(ops.subprocess, "run", spy)
    passed, _ = ops.run_tests(NAME, built["version"])
    assert passed and seen
    assert not any(k.startswith("PATCHWORK_CRED__") for k in seen[0])
    assert "SINK_TOKEN" not in seen[0] and "SOURCE_KEY" not in seen[0]
    _no_sentinel(json.dumps(seen[0]))


def test_runner_with_credentials_stays_ai_free(workspace, monkeypatch):
    monkeypatch.setenv("SINK_TOKEN", TOKEN)
    env = ops.runner_env({"sink": {"auth": {"credential": "SINK_TOKEN"}}})
    assert not any(k in env for k in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY"))
    probe = ("import sys, runner.run, runner.http, runner.credentials\n"
             "from runner.credentials import get\n"
             f"assert get('SINK_TOKEN') == {TOKEN!r}\n"
             f"leaked = sorted(m for m in sys.modules if m.split('.')[0] in {AI_MODULES!r})\n"
             "assert not leaked, leaked\n")
    r = subprocess.run([sys.executable, "-c", probe], cwd=REPO, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ---- redaction ---------------------------------------------------------------------------------------

def test_redact_helpers(monkeypatch):
    monkeypatch.setenv("SINK_TOKEN", TOKEN)
    assert credentials.redact(f"x {TOKEN} y", names=["SINK_TOKEN"]) == "x *** y"
    obj = {"a": [f"Bearer {TOKEN}", 3], TOKEN: None}
    assert credentials.redact_obj(obj, names=["SINK_TOKEN"]) == {"a": ["Bearer ***", 3], "***": None}
    big = {"html": "<p>nothing secret</p>" * 10}
    assert credentials.redact_obj(big, names=["SINK_TOKEN"]) is big  # untouched, not copied


def test_agent_tool_results_and_events_are_redacted(secure, workspace, monkeypatch):
    """Agent tools are the one way text reaches the model: results and raised errors are redacted."""
    from agents.deep_tools import ToolContext, tool_functions
    from agents.interface import _Thread
    from agents.scripted import ScriptedAgentService, work_dir

    srv, app = secure
    app.state.echo_key = True  # the source puts the key it was sent into a record body
    write_spec(workspace, srv.url)
    wd = work_dir(NAME)
    (wd / "spec.yaml").write_text((workspace / "draft" / "spec.yaml").read_text())
    svc = ScriptedAgentService(pace_s=0)
    t = _Thread(id="build-cred", mode="build", subject="x")
    svc._threads[t.id] = t
    ctx = ToolContext(svc, t, name=NAME)
    tools = tool_functions(ctx)

    out = tools["fetch_source_samples"](15)
    assert "r-echo" in out and "key=***" in out
    _no_sentinel(out)
    app.state.seen.clear()
    contract = tools["read_openapi"]("sink")  # the contract is fetched with the token, which is never shown
    assert "/api/items" in contract and app.state.seen == [f"Bearer {TOKEN}"]
    _no_sentinel(contract)

    def leaky() -> str:
        raise ValueError(f"upstream said {TOKEN}")

    from agents.deep_tools import _redacting
    with pytest.raises(RuntimeError) as e:
        _redacting(leaky, ctx)()
    assert "***" in str(e.value)
    _no_sentinel(str(e.value))

    ev = svc.emit(t, "message", agent="supervisor", text=f"the token is {TOKEN}")
    _no_sentinel(json.dumps(ev))
    assert "***" in ev["text"]
    _no_sentinel((svc.root / f"{t.id}.jsonl").read_text())
