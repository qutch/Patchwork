"""Airtable sink engine (contracts V14): spec model, contract mapping and hash, typed values, send/verify error
classification with fake time, the compiler's package, and runs against the fake Airtable. Offline."""

from __future__ import annotations

import json

import httpx
import pytest
import yaml

from control import ops
from demo_apps.fake_airtable.app import BASE_ID, REAL_BASE_FIELDS, TABLE, TOKEN, create_app
from runner import airtable
from runner.contract import openapi_hash, validate_body
from runner.lib import Quarantine
from runner.store import Store
from spec import SpecError, parse_spec
from tests.airtable.helpers import JOBS, NAME, SUBPROCESS_CREDENTIAL, airtable_sink, airtable_spec, write_spec
from tests.conftest import _Server

META = {"tables": [{"id": "tblX", "name": "Applications", "primaryFieldId": "fld1", "fields": [
    {"id": "fld1", "name": "Job Title", "type": "singleLineText", "description": "the role"},
    {"id": "fld2", "name": "Company", "type": "singleLineText"},
    {"id": "fld3", "name": "Link", "type": "url"},
    {"id": "fld4", "name": "Date Applied", "type": "date", "options": {"dateFormat": {"name": "iso"}}},
    {"id": "fld5", "name": "Email", "type": "email"},
    {"id": "fld6", "name": "Salary", "type": "currency", "options": {"precision": 0, "symbol": "$"}},
    {"id": "fld7", "name": "Remote", "type": "checkbox", "options": {"icon": "check", "color": "greenBright"}},
    {"id": "fld8", "name": "Stage", "type": "singleSelect", "options": {"choices": [
        {"id": "sel1", "name": "Applied", "color": "blue"}, {"id": "sel2", "name": "Interview"}]}},
    {"id": "fld9", "name": "Tags", "type": "multipleSelects", "options": {"choices": [{"id": "sel3", "name": "AI"}]}},
    {"id": "fld10", "name": "Applied At", "type": "dateTime"},
    {"id": "fld11", "name": "Notes", "type": "multilineText"},
    {"id": "fld12", "name": "Summary", "type": "formula", "options": {"formula": "{Job Title}"}},
    {"id": "fld13", "name": "Created", "type": "createdTime"},
    {"id": "fld14", "name": "Owner", "type": "singleCollaborator"},
    {"id": "fld15", "name": "Contacts", "type": "multipleRecordLinks"},
]}, {"id": "tblY", "name": "Other", "fields": []}]}


def props(doc):
    return doc["paths"]["/records"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]


# ---- spec ---------------------------------------------------------------------------------------

def test_airtable_spec_validates():
    spec = parse_spec(airtable_spec())
    assert spec.sink.kind == "airtable" and spec.sink.base_id == BASE_ID and spec.sink.auth.credential == "AIRTABLE_TOKEN"
    assert spec.sink_values_key() == "fields" and "Company" in spec.sink_values()


@pytest.mark.parametrize("change, msg", [
    ({"base_id": "appTooShort"}, "base_id"),
    ({"base_id": "tblAAAAAAAAAAAAAA"}, "base_id"),
    ({"table": "  "}, "table"),
    ({"fields": {}}, "at least one field"),
    ({"auth": {"scheme": "bearer", "credential": "OPENROUTER_API_KEY"}}, "not allowed"),
    ({"auth": {"scheme": "bearer", "credential": "lower_case"}}, "UPPER_SNAKE_CASE"),
    ({"fields": {"Company": {"from": "nope"}}}, "not every parser extracts"),
])
def test_airtable_spec_rejects(change, msg):
    raw = airtable_spec()
    raw["sink"].update(change)
    with pytest.raises(SpecError, match=msg):
        parse_spec(raw)


def test_airtable_spec_needs_auth():
    raw = airtable_spec()
    del raw["sink"]["auth"]
    with pytest.raises(SpecError, match="sink.auth"):
        parse_spec(raw)


# ---- contract ------------------------------------------------------------------------------------

def test_contract_type_mapping():
    doc = airtable.contract_from_tables(META["tables"], "Applications", BASE_ID)
    p = props(doc)
    assert list(p) == ["Job Title", "Company", "Link", "Date Applied", "Email", "Salary", "Remote", "Stage", "Tags",
                       "Applied At", "Notes"]  # formula, createdTime, collaborator and record links omitted
    assert p["Job Title"] == {"type": "string", "x-airtable-type": "singleLineText"}
    assert p["Link"] == {"type": "string", "format": "uri", "x-airtable-type": "url"}
    assert p["Email"]["format"] == "email" and p["Date Applied"]["format"] == "date"
    assert p["Applied At"]["format"] == "date-time"
    assert p["Salary"] == {"type": "number", "x-airtable-type": "currency"}
    assert p["Remote"]["type"] == "boolean"
    assert p["Stage"] == {"type": "string", "enum": ["Applied", "Interview"], "x-airtable-type": "singleSelect"}
    assert p["Tags"]["type"] == "array" and p["Tags"]["items"]["enum"] == ["AI"]
    schema = doc["paths"]["/records"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert schema["additionalProperties"] is False and "required" not in schema
    assert doc["info"]["x-patchwork-kind"] == "airtable"
    assert "fld1" not in json.dumps(doc) and "the role" not in json.dumps(doc)  # no field ids, no descriptions
    assert airtable.contract_from_tables(META["tables"], "tblX", BASE_ID) == doc  # by id or by name


def test_unknown_table_is_sink_contract():
    with pytest.raises(Quarantine) as e:
        airtable.contract_from_tables(META["tables"], "Nope", BASE_ID)
    assert e.value.code == "SINK_CONTRACT" and "'Applications'" in e.value.detail


def test_hash_changes_on_rename_only():
    base = airtable.contract_from_tables(META["tables"], "Applications", BASE_ID)
    tables = json.loads(json.dumps(META["tables"]))
    tables[0]["fields"][1]["id"] = "fldNEWID"
    tables[0]["fields"][1]["description"] = "who hires"
    tables[0]["fields"][6]["options"]["color"] = "redBright"  # cosmetic options (colours) are not in the contract
    same = airtable.contract_from_tables(tables, "Applications", BASE_ID)
    assert openapi_hash(same) == openapi_hash(base)
    tables[0]["fields"][1]["name"] = "Employer"
    renamed = airtable.contract_from_tables(tables, "Applications", BASE_ID)
    assert openapi_hash(renamed) != openapi_hash(base)
    body = {"Job Title": "SWE", "Company": "Acme"}
    assert validate_body(base, "POST", "/records", body) == []
    assert validate_body(renamed, "POST", "/records", body) == [
        "body.Company: not accepted by destination (no such property)"]


def test_fetch_contract_from_the_fake_is_stable():
    with _Server(create_app()) as srv, pytest.MonkeyPatch.context() as mp:
        mp.setenv("PATCHWORK_AIRTABLE_API_URL", srv.url + "/v0")
        h = {"Authorization": f"Bearer {TOKEN}"}
        a = airtable.fetch_contract(BASE_ID, TABLE, h)
        b = airtable.fetch_contract(BASE_ID, TABLE, h)
        assert openapi_hash(a) == openapi_hash(b)
        assert list(props(a)) == ["Job Title", "Company", "Link", "Date Applied", "Email"]  # formula Summary omitted
        with pytest.raises(Quarantine) as e:
            airtable.fetch_contract(BASE_ID, TABLE, {"Authorization": "Bearer wrong-token-123"}, credential="AIRTABLE_TOKEN")
        assert e.value.code == "AUTH" and "AIRTABLE_TOKEN" in e.value.detail and "wrong-token" not in e.value.detail


# ---- typed values --------------------------------------------------------------------------------

def test_convert_body_types_values():
    doc = airtable.contract_from_tables(META["tables"], "Applications", BASE_ID)
    out = airtable.convert_body(doc, {"Salary": "120,000", "Remote": "yes", "Date Applied": "2026-11-14T10:00:00Z",
                                      "Stage": "Applied", "Tags": "AI", "Job Title": 7, "Notes": "", "Email": None})
    assert out == {"Salary": 120000, "Remote": True, "Date Applied": "2026-11-14", "Stage": "Applied", "Tags": ["AI"],
                   "Job Title": "7"}
    kept = airtable.convert_body(doc, {"Notes": "", "Email": None}, drop_empty=False)
    assert kept == {"Notes": "", "Email": None}


@pytest.mark.parametrize("body", [{"Salary": "lots"}, {"Remote": "maybe"}, {"Date Applied": "14/11/2026"},
                                  {"Stage": "Hired"}, {"Tags": ["ML"]}, {"Salary": True}])
def test_value_that_does_not_fit_is_validation_failed(body):
    doc = airtable.contract_from_tables(META["tables"], "Applications", BASE_ID)
    with pytest.raises(Quarantine) as e:
        airtable.convert_body(doc, body)
    assert e.value.code == "VALIDATION_FAILED" and list(body)[0] in e.value.detail


def test_typecast_allows_new_choices():
    doc = airtable.contract_from_tables(META["tables"], "Applications", BASE_ID)
    assert airtable.convert_body(doc, {"Stage": "Hired"}, typecast=True) == {"Stage": "Hired"}


# ---- send / verify (MockTransport, fake time) -----------------------------------------------------

class FakeTime:
    def __init__(self):
        self.now, self.sleeps = 1000.0, []

    def clock(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(round(s, 3))
        self.now += s


def mock_client(responses, seen):
    it = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, json.loads(request.content) if request.content else None,
                     request.headers.get("authorization")))
        status, data = next(it)
        return httpx.Response(status, json=data)
    return httpx.Client(transport=httpx.MockTransport(handler))


H = {"Authorization": "Bearer sentinel-token-value-42"}


def test_send_posts_one_record_and_returns_its_id():
    seen, ft = [], FakeTime()
    pacer = airtable.Pacer(clock=ft.clock, sleep=ft.sleep)
    client = mock_client([(200, {"records": [{"id": "recAAA", "fields": {}}]})], seen)
    rid = airtable.send(BASE_ID, "Applications", {"Company": "Acme"}, H, client=client, pacer=pacer)
    assert rid == "recAAA"
    method, path, body, auth = seen[0]
    assert (method, path) == ("POST", f"/v0/{BASE_ID}/Applications")
    assert body == {"records": [{"fields": {"Company": "Acme"}}], "typecast": False} and auth == H["Authorization"]


@pytest.mark.parametrize("status, data, code, text", [
    (422, {"error": {"type": "UNKNOWN_FIELD_NAME", "message": 'Unknown field name: "Company"'}}, "SINK_CONTRACT",
     "UNKNOWN_FIELD_NAME"),
    (422, {"error": {"type": "INVALID_VALUE_FOR_COLUMN", "message": "Field Link cannot accept"}}, "SINK_CONTRACT",
     "INVALID_VALUE_FOR_COLUMN"),
    (401, {"error": {"type": "AUTHENTICATION_REQUIRED"}}, "AUTH", "AIRTABLE_TOKEN"),
    (403, {"error": {"type": "INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND"}}, "AUTH", "HTTP 403"),
    (503, {"error": "SERVICE_UNAVAILABLE"}, "UNKNOWN", "503"),
])
def test_send_error_classification(status, data, code, text):
    seen, ft = [], FakeTime()
    client = mock_client([(status, data)], seen)
    with pytest.raises(Quarantine) as e:
        airtable.send(BASE_ID, TABLE, {"Company": "Acme"}, H, client=client, credential="AIRTABLE_TOKEN",
                      pacer=airtable.Pacer(clock=ft.clock, sleep=ft.sleep))
    assert e.value.code == code and text in e.value.detail
    assert "sentinel-token-value-42" not in e.value.detail


def test_429_waits_30s_and_retries_once():
    seen, ft = [], FakeTime()
    pacer = airtable.Pacer(clock=ft.clock, sleep=ft.sleep)
    client = mock_client([(429, {"errors": [{"error": "RATE_LIMIT_REACHED"}]}), (200, {"records": [{"id": "recB"}]})], seen)
    assert airtable.send(BASE_ID, TABLE, {"Company": "Acme"}, H, client=client, pacer=pacer) == "recB"
    assert 30.0 in ft.sleeps and len(seen) == 2
    seen2 = []
    client = mock_client([(429, {}), (429, {})], seen2)
    with pytest.raises(Quarantine) as e:
        airtable.send(BASE_ID, TABLE, {"Company": "Acme"}, H, client=client, pacer=pacer)
    assert e.value.code == "UNKNOWN" and "429" in e.value.detail and len(seen2) == 2


def test_requests_are_paced_per_base():
    ft = FakeTime()
    pacer = airtable.Pacer(clock=ft.clock, sleep=ft.sleep)
    pacer.wait("appA")
    ft.now += 0.05
    pacer.wait("appA")
    assert ft.sleeps == [0.17]  # 0.22 s apart
    pacer.wait("appB")  # another base has its own budget
    assert ft.sleeps == [0.17]
    ft.now += 1.0
    pacer.wait("appA")
    assert ft.sleeps == [0.17]


def test_verify_read_back():
    doc = airtable.contract_from_tables(META["tables"], "Applications", BASE_ID)
    sent = {"Job Title": "Senior  Engineer", "Salary": 120000, "Date Applied": "2026-11-14", "Remote": False}
    ft = FakeTime()
    pacer = airtable.Pacer(clock=ft.clock, sleep=ft.sleep)
    ok = {"id": "recA", "fields": {"Job Title": "Senior Engineer", "Salary": 120000.0, "Date Applied": "2026-11-14"}}
    airtable.verify_read_back(BASE_ID, TABLE, "recA", sent, H, doc, client=mock_client([(200, ok)], []), pacer=pacer)
    bad = {"id": "recA", "fields": {"Job Title": "Senior Engineer", "Salary": 12000, "Date Applied": "2026-11-15"}}
    with pytest.raises(Quarantine) as e:
        airtable.verify_read_back(BASE_ID, TABLE, "recA", sent, H, doc, client=mock_client([(200, bad)], []), pacer=pacer)
    assert e.value.code == "VERIFY_MISMATCH" and "Salary" in e.value.detail and "Date Applied" in e.value.detail
    with pytest.raises(Quarantine) as e:
        airtable.verify_read_back(BASE_ID, TABLE, "recA", sent, H, doc, pacer=pacer,
                                  client=mock_client([(404, {"error": "NOT_FOUND"})], []))
    assert e.value.code == "VERIFY_MISMATCH"


def test_dry_run_session_never_posts():
    seen = []
    meta = {"tables": [{"id": "tblX", "name": TABLE, "fields": META["tables"][0]["fields"][:5]}]}
    ft = FakeTime()
    s = airtable.AirtableSession(BASE_ID, TABLE, H, dry_run=True, client=mock_client([(200, meta)], seen),
                                 pacer=airtable.Pacer(clock=ft.clock, sleep=ft.sleep))
    res = s.submit({"Job Title": "SWE", "Company": "Acme"})
    assert res.status_code == 200 and "not sent" in res.detail
    assert [m for m, *_ in seen] == ["GET"] and s.posts == 0
    with pytest.raises(Quarantine) as e:
        s.submit({"Employer": "Acme"})
    assert e.value.code == "SINK_CONTRACT" and "Employer" in e.value.detail


# ---- the fake ------------------------------------------------------------------------------------

def test_fake_airtable_api():
    with _Server(create_app()) as srv:
        h = {"Authorization": f"Bearer {TOKEN}"}
        assert httpx.get(f"{srv.url}/v0/meta/bases/{BASE_ID}/tables").status_code == 401
        assert httpx.post(f"{srv.url}/v0/{BASE_ID}/{TABLE}", headers={"Authorization": "Bearer nope"},
                          json={"records": [{"fields": {}}]}).status_code == 401
        r = httpx.post(f"{srv.url}/v0/{BASE_ID}/{TABLE}", headers=h, json={"records": [{"fields": {"Employer": "x"}}]})
        assert r.status_code == 422 and r.json()["error"]["type"] == "UNKNOWN_FIELD_NAME"
        r = httpx.post(f"{srv.url}/v0/{BASE_ID}/{TABLE}", headers=h, json={"records": [{"fields": {"Date Applied": "Nov 14"}}]})
        assert r.status_code == 422 and r.json()["error"]["type"] == "INVALID_VALUE_FOR_COLUMN"
        r = httpx.post(f"{srv.url}/v0/{BASE_ID}/{TABLE}", headers=h, json={"records": [{"fields": {"Summary": "x"}}]})
        assert r.status_code == 422 and "computed" in r.json()["error"]["message"]
        r = httpx.post(f"{srv.url}/v0/{BASE_ID}/{TABLE}", headers=h,
                       json={"records": [{"fields": {"Job Title": "SWE", "Company": "Acme"}}]})
        rid = r.json()["records"][0]["id"]
        got = httpx.get(f"{srv.url}/v0/{BASE_ID}/tblApplications01/{rid}", headers=h).json()
        assert got["fields"] == {"Job Title": "SWE", "Company": "Acme", "Summary": "SWE at Acme"}
        assert httpx.post(f"{srv.url}/admin/rename", json={"table": TABLE, "from": "Company", "to": "Employer"}).status_code == 200
        assert httpx.get(f"{srv.url}/admin/records").json()[0]["Employer"] == "Acme"
        httpx.post(f"{srv.url}/admin/reset")
        assert httpx.get(f"{srv.url}/admin/records").json() == []
    with _Server(create_app(fields=REAL_BASE_FIELDS)) as srv:
        meta = httpx.get(f"{srv.url}/v0/meta/bases/{BASE_ID}/tables", headers={"Authorization": f"Bearer {TOKEN}"}).json()
        assert [f["name"] for f in meta["tables"][0]["fields"]] == ["Job Title", "Company", "Link", "Date Applied", "Summary"]


# ---- compile + runs against the fake ----------------------------------------------------------------

@pytest.fixture
def fake_airtable(monkeypatch):
    with _Server(create_app()) as srv:
        monkeypatch.setenv("PATCHWORK_AIRTABLE_API_URL", srv.url + "/v0")
        yield srv.url


@pytest.fixture
def compiled(workspace, fake_airtable, monkeypatch):
    """jobs_to_tracker v1 into the fake table, compiled against its live contract, tested and deployed. AIRTABLE_TOKEN
    is in the control plane's env; runner_env(spec) hands it to the runner as PATCHWORK_CRED__AIRTABLE_TOKEN."""
    monkeypatch.setenv(SUBPROCESS_CREDENTIAL, TOKEN)
    path = write_spec(workspace, airtable_spec(credential=SUBPROCESS_CREDENTIAL))
    built = ops.compile_and_test(path, examples_path=JOBS / "examples.yaml")
    assert built["tests_passed"], built["tests_summary"]
    assert built["contract_errors"] == []
    ops.deploy(NAME, built["version"])
    return {"fake": fake_airtable, "built": built, "vdir": ops.version_dir(NAME, built["version"])}


@pytest.fixture
def inbox(monkeypatch):
    from tests.engine.fake_inbox import create_app as inbox_app
    with _Server(inbox_app()) as srv:
        monkeypatch.setenv("PATCHWORK_CONTROL_URL", srv.url)
        yield srv.url


def records(url):
    return httpx.get(url + "/admin/records").json()


def test_compiled_package(compiled):
    vdir = compiled["vdir"]
    manifest = json.loads((vdir / "manifest.json").read_text())
    assert manifest["sink_call"] == {"method": "POST", "path": "/records"} and manifest["contract_kind"] == "airtable"
    config = yaml.safe_load((vdir / "config.yaml").read_text())
    assert config["sink"] == {"kind": "airtable", "base_id": BASE_ID, "table": TABLE,
                              "auth": {"scheme": "bearer", "credential": SUBPROCESS_CREDENTIAL},
                              "typecast": False, "read_back": True}
    every = "".join(p.read_text() for p in vdir.rglob("*") if p.is_file() and p.suffix in (".py", ".json", ".yaml"))
    assert TOKEN not in every
    assert "Summary" not in props(json.loads((vdir / "contract" / "openapi.json").read_text()))
    assert "FIELD_TYPES = {" in (vdir / "adapters.py").read_text()


def test_compile_is_deterministic(workspace, compiled):
    from compiler.compile import compile_spec, load_examples
    from spec import load_spec
    spec = load_spec(workspace / "spec.yaml")
    contract = json.loads((compiled["vdir"] / "contract" / "openapi.json").read_text())
    a = compile_spec(spec, version=90, out_root=workspace / "c", examples=load_examples(JOBS / "examples.yaml"),
                     sink_openapi=contract)
    b = compile_spec(spec, version=91, out_root=workspace / "c", examples=load_examples(JOBS / "examples.yaml"),
                     sink_openapi=contract)
    for rel in ("adapters.py", "parsers.py", "tests/test_golden.py"):
        assert (a.path / rel).read_text().replace("v90", "vN") == (b.path / rel).read_text().replace("v91", "vN")


def test_run_writes_records_and_reads_them_back(compiled, inbox):
    s = ops.run_sandboxed(NAME)
    assert s["status"] == "completed" and s["llm_calls"] == 0, s
    assert s["counts"] == {"success": 6, "quarantined": 2, "quarantined:PARSE_NO_MATCH": 2}, s
    rows = records(compiled["fake"])
    assert len(rows) == 6
    by_title = {r["Job Title"]: r for r in rows}
    assert by_title["American Tech Fellowship"]["Company"] == "Palantir Technologies"
    assert by_title["American Tech Fellowship"]["Summary"] == "American Tech Fellowship at Palantir Technologies"
    assert all(r["Email"] == "demo@example.com" and r["Date Applied"] == "2026-11-14" for r in rows)
    again = ops.run_sandboxed(NAME)
    assert again["counts"] == {} and len(records(compiled["fake"])) == 6


def test_rename_blocks_before_any_write(compiled, inbox):
    httpx.post(compiled["fake"] + "/admin/rename", json={"table": TABLE, "from": "Company", "to": "Employer"})
    s = ops.run_sandboxed(NAME)
    assert s["status"] == "blocked" and s["exit_code"] == 2, s
    assert records(compiled["fake"]) == []
    inc = Store().incident(s["incident_id"])
    assert inc["type"] == "CONTRACT_CHANGED"
    errors = inc["detail"]["contract_errors"]
    assert errors and all(e.endswith("body.Company: not accepted by destination (no such property)") for e in errors)
    assert inc["detail"]["sink_call"] == {"method": "POST", "path": "/records"}
    assert "Employer" in props(inc["new_openapi"]) and "Company" in props(inc["old_openapi"])
    assert TOKEN not in json.dumps(inc)


def test_dry_run_never_posts(compiled, inbox):
    s = ops.run_sandboxed(NAME, dry_run=True)
    assert s["status"] == "completed" and s["counts"]["success"] == 6, s
    assert records(compiled["fake"]) == []
    assert {r["detail"] for r in Store().records(s["run_id"]) if r["status"] == "success"} == {
        "dry run: validated against the table, not sent"}


def test_wrong_token_fails_without_writing_or_leaking(compiled, inbox, monkeypatch):
    monkeypatch.setenv(SUBPROCESS_CREDENTIAL, "sentinel-wrong-token-77")
    s = ops.run_sandboxed(NAME)
    assert s["status"] == "failed" and "AUTH" in s["message"] and SUBPROCESS_CREDENTIAL in s["message"], s
    assert "sentinel-wrong-token-77" not in json.dumps(s)
    assert records(compiled["fake"]) == []


def test_missing_credential_fails_the_run_naming_it(compiled, inbox, monkeypatch):
    monkeypatch.delenv(SUBPROCESS_CREDENTIAL)
    s = ops.run_sandboxed(NAME)
    assert s["status"] == "failed" and f"credential {SUBPROCESS_CREDENTIAL} is not set" in s["message"], s
    assert s.get("run_id") is None  # runner_env refuses before the runner starts (V13)


def test_in_process_run_with_airtable_token(workspace, fake_airtable, inbox, monkeypatch):
    """The runner process itself with PATCHWORK_CRED__AIRTABLE_TOKEN (what runner_env(spec) passes): a page run
    writes, a rename is CONTRACT_CHANGED before any write."""
    from runner.run import run
    monkeypatch.setenv("PATCHWORK_CRED__AIRTABLE_TOKEN", TOKEN)
    monkeypatch.delenv("AIRTABLE_TOKEN", raising=False)
    built = ops.compile_and_test(write_spec(workspace, airtable_spec()), examples_path=JOBS / "examples.yaml")
    assert built["tests_passed"], built["tests_summary"]
    ops.deploy(NAME, built["version"])
    lines = []
    s = run(NAME, limit=2, log=lines.append)
    assert s["status"] == "completed" and s["counts"] == {"success": 2}, s
    assert len(records(fake_airtable)) == 2
    httpx.post(fake_airtable + "/admin/rename", json={"from": "Company", "to": "Employer"})
    s = run(NAME, log=lines.append)
    assert s["status"] == "blocked" and len(records(fake_airtable)) == 2
    assert Store().incident(s["incident_id"])["type"] == "CONTRACT_CHANGED"
    assert TOKEN not in "".join(lines)


def test_value_that_does_not_fit_is_quarantined_before_any_request(workspace, fake_airtable, monkeypatch):
    """build_body types values with the compile-time field types: a Date Applied that is not a date is
    VALIDATION_FAILED and nothing is sent."""
    from runner.loader import load_integration
    from runner.pipeline import MemoryStore, process_record
    from runner.testing import FakeHttp
    monkeypatch.setenv("AIRTABLE_TOKEN", TOKEN)
    raw = airtable_spec()
    raw["sink"]["fields"]["Date Applied"] = {"from": "record.title"}
    built = ops.compile_and_test(write_spec(workspace, raw), examples_path=JOBS / "examples.yaml")
    integ = load_integration(built["path"])
    ex = yaml.safe_load((JOBS / "examples.yaml").read_text())[0]
    http = FakeHttp()
    out = process_record(integ, ex["record"], http, MemoryStore())
    assert (out.status, out.reason) == ("quarantined", "VALIDATION_FAILED") and "Date Applied" in out.detail
    assert http.sink_writes == []


def test_control_fetch_contract_for_airtable(fake_airtable, monkeypatch):
    monkeypatch.setenv("AIRTABLE_TOKEN", TOKEN)
    doc = ops.fetch_contract(parse_spec(airtable_spec()))
    assert list(props(doc)) == ["Job Title", "Company", "Link", "Date Applied", "Email"]
    monkeypatch.setenv("AIRTABLE_TOKEN", "sentinel-bad-token-99")
    with pytest.raises(RuntimeError) as e:
        ops.fetch_contract(parse_spec(airtable_spec()))
    assert "AUTH" in str(e.value) and "sentinel-bad-token-99" not in str(e.value)


def test_form_and_api_packages_unchanged(workspace):
    """The airtable branch does not touch form/API code generation: v1's adapters still come from adapters.py.j2."""
    from compiler.compile import compile_spec, load_examples
    from spec import load_spec
    res = compile_spec(load_spec(JOBS / "spec.yaml"), version=1, out_root=workspace / "c",
                       examples=load_examples(JOBS / "examples.yaml"))
    text = (res.path / "integration.py").read_text()
    assert "adapters.typed" not in text and "http.form_submit" in (res.path / "adapters.py").read_text()
    assert yaml.safe_load((res.path / "config.yaml").read_text())["sink"]["kind"] == "form"
    assert airtable_sink()["kind"] == "airtable"
