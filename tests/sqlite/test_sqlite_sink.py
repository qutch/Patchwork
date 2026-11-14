"""SQLite sink engine (contracts V16): spec model, contract mapping and hash, typed values, send/verify error
classification, path confinement, SQL safety, the compiler's package, and runs against a database file. Offline."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing

import httpx  # noqa: F401  (inbox fixture talks to the fake inbox)
import pytest
import yaml

from control import ops
from demo_apps.sqlite_tracker.db import TABLE, create_db, rename_column, rows
from runner import sqlite_sink
from runner.contract import openapi_hash, validate_body
from runner.lib import Quarantine
from runner.store import Store
from spec import SpecError, parse_spec
from tests.conftest import _Server
from tests.sqlite.helpers import DB, JOBS, NAME, sqlite_sink as sink_dict, sqlite_spec, write_spec


def props(doc):
    return doc["paths"]["/rows"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]


def required(doc):
    return doc["paths"]["/rows"]["post"]["requestBody"]["content"]["application/json"]["schema"].get("required", [])


@pytest.fixture
def db(workspace):
    create_db(workspace / DB)
    return DB


def sql(workspace, statement, *args):
    with closing(sqlite3.connect(workspace / DB, isolation_level=None)) as conn:
        return conn.execute(statement, args).fetchall()


# ---- spec ---------------------------------------------------------------------------------------

def test_sqlite_spec_validates():
    spec = parse_spec(sqlite_spec())
    assert spec.sink.kind == "sqlite" and spec.sink.path == DB and spec.sink_values_key() == "fields"


@pytest.mark.parametrize("change,needle", [
    ({"path": "/etc/app.db"}, "relative"),
    ({"path": "../outside.db"}, "'..'"),
    ({"path": "a/../../b.db"}, "'..'"),
    ({"path": "~/x.db"}, "relative"),
    ({"path": " "}, "database file"),
    ({"table": " "}, "table name"),
    ({"fields": {}}, "at least one column"),
    ({"fields": {" ": {"const": "x"}}}, "must not be empty"),
    ({"auth": {"scheme": "bearer", "credential": "X"}}, "auth"),  # a local file has no credential
])
def test_sqlite_spec_rejects(change, needle):
    raw = sqlite_spec()
    raw["sink"].update(change)
    with pytest.raises(SpecError) as e:
        parse_spec(raw)
    assert needle in str(e.value)


# ---- contract -----------------------------------------------------------------------------------

def test_contract_from_pragma(workspace, db):
    doc = sqlite_sink.fetch_contract(DB, TABLE)
    assert doc["info"]["x-patchwork-kind"] == "sqlite" and doc["info"]["x-sqlite-path"] == DB
    p = props(doc)
    assert list(p) == ["id", "Email", "Job Title", "Company", "Link", "Date Applied"]
    assert p["id"] == {"type": "integer", "x-sqlite-type": "INTEGER"}
    assert p["Date Applied"] == {"type": "string", "format": "date", "x-sqlite-type": "DATE"}
    assert required(doc) == ["Email"]  # NOT NULL without a default; the rowid alias is not required


def test_type_mapping_and_skipped_columns(workspace):
    path = workspace / "t.db"
    with closing(sqlite3.connect(path, isolation_level=None)) as c:
        c.execute('CREATE TABLE t (a INT, b REAL, c NUMERIC(10,2), d BOOLEAN, e DATETIME, f TIMESTAMP, g VARCHAR(20), '
                  'h BLOB, i, j TEXT DEFAULT "x" NOT NULL, k INT GENERATED ALWAYS AS (a * 2) VIRTUAL)')
    p = props(sqlite_sink.fetch_contract("t.db", "t"))
    assert {k: v["type"] for k, v in p.items()} == {"a": "integer", "b": "number", "c": "number", "d": "boolean",
                                                    "e": "string", "f": "string", "g": "string", "i": "string",
                                                    "j": "string"}
    assert p["e"]["format"] == "date-time" and p["f"]["format"] == "date-time"
    assert "h" not in p and "k" not in p  # BLOB and generated columns are not writable
    assert required(sqlite_sink.fetch_contract("t.db", "t")) == []  # j has a default


def test_unknown_table_lists_the_tables(workspace, db):
    with pytest.raises(Quarantine) as e:
        sqlite_sink.fetch_contract(DB, "Nope")
    assert e.value.code == "SINK_CONTRACT" and "Applications" in e.value.detail


def test_hash_changes_on_rename_and_retype_only(workspace, db):
    base = openapi_hash(sqlite_sink.fetch_contract(DB, TABLE))
    sql(workspace, 'INSERT INTO Applications (Email) VALUES ("a@b.co")')  # data is not part of the contract
    assert openapi_hash(sqlite_sink.fetch_contract(DB, TABLE)) == base
    sql(workspace, "CREATE INDEX i1 ON Applications (Company)")
    assert openapi_hash(sqlite_sink.fetch_contract(DB, TABLE)) == base
    rename_column(workspace / DB, "Company", "Employer")
    renamed = sqlite_sink.fetch_contract(DB, TABLE)
    assert openapi_hash(renamed) != base and "Employer" in props(renamed) and "Company" not in props(renamed)


# ---- path confinement ---------------------------------------------------------------------------

def test_missing_database_is_not_created(workspace):
    with pytest.raises(Quarantine) as e:
        sqlite_sink.fetch_contract("nothing.db", TABLE)
    assert e.value.code == "SINK_CONTRACT" and "not found" in e.value.detail
    assert not (workspace / "nothing.db").exists()


@pytest.mark.parametrize("path", ["../escape.db", "sub/../../escape.db"])
def test_path_outside_workspace_is_refused(workspace, tmp_path_factory, path):
    outside = tmp_path_factory.mktemp("out")
    ws = workspace / "ws"
    ws.mkdir()
    os.environ["PATCHWORK_WORKSPACE"] = str(ws)
    create_db(ws / ".." / "escape.db")
    with pytest.raises(Quarantine) as e:
        sqlite_sink.fetch_contract(path, TABLE)
    assert e.value.code == "SINK_CONTRACT" and "leaves the workspace" in e.value.detail
    assert outside.exists()


def test_symlink_out_of_workspace_is_refused(workspace, tmp_path_factory):
    ws = workspace / "ws"
    ws.mkdir()
    os.environ["PATCHWORK_WORKSPACE"] = str(ws)
    elsewhere = tmp_path_factory.mktemp("elsewhere") / "x.db"
    create_db(elsewhere)
    (ws / "link.db").symlink_to(elsewhere)
    with pytest.raises(Quarantine) as e:
        sqlite_sink.fetch_contract("link.db", TABLE)
    assert "leaves the workspace" in e.value.detail


# ---- typed values -------------------------------------------------------------------------------

def test_convert_values(workspace, db):
    p = props(sqlite_sink.fetch_contract(DB, TABLE))
    out = sqlite_sink.convert_values(p, {"Email": "a@b.co", "Date Applied": "2026-11-14T10:00:00Z", "Company": "",
                                         "id": "7"})
    assert out == {"Email": "a@b.co", "Date Applied": "2026-11-14", "id": 7}  # empty dropped, int typed
    kept = sqlite_sink.convert_values(p, {"Company": ""}, drop_empty=False)
    assert kept == {"Company": ""}
    with pytest.raises(Quarantine) as e:
        sqlite_sink.convert_values(p, {"Date Applied": "yesterday", "id": "1.5"})
    assert e.value.code == "VALIDATION_FAILED" and "Date Applied" in e.value.detail and "id" in e.value.detail
    assert "SQLite column" in e.value.detail


# ---- send / read back ---------------------------------------------------------------------------

def session(**kw):
    return sqlite_sink.SqliteSession(DB, TABLE, **kw)


def test_submit_inserts_and_reads_back(workspace, db):
    s = session()
    r = s.submit({"Email": "a@b.co", "Job Title": "SWE", "Company": "Acme", "Link": "http://x", "Date Applied": "2026-11-14"})
    assert r.status_code == 200 and r.rowid == 1 and s.inserts == 1
    assert rows(workspace / DB) == [{"Email": "a@b.co", "Job Title": "SWE", "Company": "Acme", "Link": "http://x",
                                     "Date Applied": "2026-11-14"}]


def test_unknown_column_is_sink_contract_and_writes_nothing(workspace, db):
    with pytest.raises(Quarantine) as e:
        session().submit({"Email": "a@b.co", "Salary": "9"})
    assert e.value.code == "SINK_CONTRACT" and "Salary" in e.value.detail
    assert rows(workspace / DB) == []


def test_missing_required_column_is_sink_contract(workspace, db):
    with pytest.raises(Quarantine) as e:
        session().submit({"Job Title": "SWE"})
    assert e.value.code == "SINK_CONTRACT" and "Email" in e.value.detail and "required" in e.value.detail


def test_constraint_violation_is_validation_failed(workspace, db):
    sql(workspace, 'CREATE UNIQUE INDEX uq ON Applications (Email)')
    session().submit({"Email": "a@b.co"})
    with pytest.raises(Quarantine) as e:
        session().submit({"Email": "a@b.co"})
    assert e.value.code == "VALIDATION_FAILED" and "UNIQUE" in e.value.detail
    assert len(rows(workspace / DB)) == 1


def test_check_constraint_is_validation_failed(workspace):
    with closing(sqlite3.connect(workspace / "c.db", isolation_level=None)) as c:
        c.execute("CREATE TABLE t (n INTEGER CHECK (n > 0))")
    with pytest.raises(Quarantine) as e:
        sqlite_sink.SqliteSession("c.db", "t").submit({"n": -1})
    assert e.value.code == "VALIDATION_FAILED"


def test_locked_database_is_unknown(workspace, db, monkeypatch):
    monkeypatch.setattr(sqlite_sink, "BUSY_TIMEOUT_S", 0.1)
    blocker = sqlite3.connect(workspace / DB, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(Quarantine) as e:
            session(read_back=False).submit({"Email": "a@b.co"})
        assert e.value.code == "UNKNOWN" and "locked" in e.value.detail
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()


def test_read_back_mismatch(workspace, db):
    """A trigger that rewrites the value after the insert: the row is not what was sent."""
    sql(workspace, "CREATE TRIGGER t1 AFTER INSERT ON Applications BEGIN "
                   "UPDATE Applications SET Company = 'Changed' WHERE id = NEW.id; END")
    with pytest.raises(Quarantine) as e:
        session().submit({"Email": "a@b.co", "Company": "Acme"})
    assert e.value.code == "VERIFY_MISMATCH" and "Company" in e.value.detail
    assert session(read_back=False).submit({"Email": "b@b.co", "Company": "Acme"}).status_code == 200


def test_boolean_and_number_columns(workspace):
    with closing(sqlite3.connect(workspace / "n.db", isolation_level=None)) as c:
        c.execute("CREATE TABLE t (n INTEGER, x REAL, ok BOOLEAN)")
    r = sqlite_sink.SqliteSession("n.db", "t").submit({"n": "1,200", "x": "2.50", "ok": "yes"})
    assert r.rowid == 1
    with closing(sqlite3.connect(workspace / "n.db")) as c:
        assert c.execute("SELECT n, x, ok FROM t").fetchall() == [(1200, 2.5, 1)]


def test_dry_run_never_writes_and_uses_a_read_only_connection(workspace, db, monkeypatch):
    modes = []
    real = sqlite_sink._connect
    monkeypatch.setattr(sqlite_sink, "_connect", lambda p, *, readonly: (modes.append(readonly), real(p, readonly=readonly))[1])
    s = session(dry_run=True)
    r = s.submit({"Email": "a@b.co"})
    assert r.status_code == 200 and "dry run" in r.detail and s.inserts == 0
    assert rows(workspace / DB) == [] and modes == [True]
    with pytest.raises(Quarantine):  # a dry run still checks the contract
        s.submit({"Email": "a@b.co", "Nope": "x"})


# ---- SQL safety ---------------------------------------------------------------------------------

def test_values_are_bound_not_spliced(workspace, db):
    evil = "x'); DROP TABLE Applications; --"
    session().submit({"Email": evil})
    assert rows(workspace / DB)[0]["Email"] == evil
    assert sql(workspace, "SELECT count(*) FROM Applications") == [(1,)]


def test_column_names_with_quotes_are_escaped(workspace):
    with closing(sqlite3.connect(workspace / "q.db", isolation_level=None)) as c:
        c.execute('CREATE TABLE "my ""t""" ("a ""b""" TEXT)')
    sqlite_sink.SqliteSession("q.db", 'my "t"').submit({'a "b"': "v"})
    with closing(sqlite3.connect(workspace / "q.db")) as c:
        assert c.execute('SELECT * FROM "my ""t"""').fetchall() == [("v",)]


def test_field_name_that_is_not_a_column_never_reaches_sql(workspace, db):
    contract = sqlite_sink.fetch_contract(DB, TABLE)
    with pytest.raises(Quarantine) as e:
        sqlite_sink.send(DB, TABLE, {'Email") VALUES (1); DROP TABLE Applications; --': "x"}, contract)
    assert e.value.code == "SINK_CONTRACT"
    assert sql(workspace, "SELECT count(*) FROM Applications") == [(0,)]


# ---- compile + runs against the file ----------------------------------------------------------------

@pytest.fixture
def compiled(workspace, db):
    """jobs_to_tracker v1 into the table, compiled against its live contract, tested and deployed."""
    path = write_spec(workspace, sqlite_spec())
    built = ops.compile_and_test(path, examples_path=JOBS / "examples.yaml")
    assert built["tests_passed"], built["tests_summary"]
    assert built["contract_errors"] == []
    ops.deploy(NAME, built["version"])
    return {"built": built, "vdir": ops.version_dir(NAME, built["version"]), "db": workspace / DB}


@pytest.fixture
def inbox(monkeypatch):
    from tests.engine.fake_inbox import create_app as inbox_app
    with _Server(inbox_app()) as srv:
        monkeypatch.setenv("PATCHWORK_CONTROL_URL", srv.url)
        yield srv.url


def test_compiled_package(compiled):
    vdir = compiled["vdir"]
    manifest = json.loads((vdir / "manifest.json").read_text())
    assert manifest["sink_call"] == {"method": "POST", "path": "/rows"} and manifest["contract_kind"] == "sqlite"
    config = yaml.safe_load((vdir / "config.yaml").read_text())
    assert config["sink"] == {"kind": "sqlite", "path": DB, "table": TABLE, "read_back": True}
    assert config["credentials"] == {} and "credentials" not in manifest
    adapters = (vdir / "adapters.py").read_text()
    assert "FIELD_TYPES = {" in adapters and "SQLITE = " in adapters and "airtable" not in adapters.lower()
    assert "Date Applied" in props(json.loads((vdir / "contract" / "openapi.json").read_text()))


def test_compile_is_deterministic(workspace, compiled):
    from compiler.compile import compile_spec, load_examples
    from spec import load_spec
    spec = load_spec(workspace / "spec.yaml")
    contract = json.loads((compiled["vdir"] / "contract" / "openapi.json").read_text())
    a = compile_spec(spec, version=90, out_root=workspace / "c", examples=load_examples(JOBS / "examples.yaml"), sink_openapi=contract)
    b = compile_spec(spec, version=91, out_root=workspace / "c", examples=load_examples(JOBS / "examples.yaml"), sink_openapi=contract)
    for rel in ("adapters.py", "parsers.py", "tests/test_golden.py"):
        assert (a.path / rel).read_text().replace("v90", "vN") == (b.path / rel).read_text().replace("v91", "vN")


def test_run_writes_rows_and_reads_them_back(compiled, inbox):
    s = ops.run_sandboxed(NAME)
    assert s["status"] == "completed" and s["llm_calls"] == 0, s
    assert s["counts"] == {"success": 6, "quarantined": 2, "quarantined:PARSE_NO_MATCH": 2}, s
    got = rows(compiled["db"])
    assert len(got) == 6
    assert {r["Job Title"]: r["Company"] for r in got}["American Tech Fellowship"] == "Palantir Technologies"
    assert all(r["Email"] == "demo@example.com" and r["Date Applied"] == "2026-11-14" for r in got)
    again = ops.run_sandboxed(NAME)
    assert again["counts"] == {} and len(rows(compiled["db"])) == 6


def test_rename_blocks_before_any_write(compiled, inbox):
    rename_column(compiled["db"], "Company", "Employer")
    s = ops.run_sandboxed(NAME)
    assert s["status"] == "blocked" and s["exit_code"] == 2, s
    assert rows(compiled["db"]) == []
    inc = Store().incident(s["incident_id"])
    assert inc["type"] == "CONTRACT_CHANGED"
    errors = inc["detail"]["contract_errors"]
    assert errors and all(e.endswith("body.Company: not accepted by destination (no such property)") for e in errors)
    assert inc["detail"]["sink_call"] == {"method": "POST", "path": "/rows"}
    assert "Employer" in props(inc["new_openapi"]) and "Company" in props(inc["old_openapi"])


def test_dry_run_never_inserts(compiled, inbox):
    s = ops.run_sandboxed(NAME, dry_run=True)
    assert s["status"] == "completed" and s["counts"]["success"] == 6, s
    assert rows(compiled["db"]) == []
    assert {r["detail"] for r in Store().records(s["run_id"]) if r["status"] == "success"} == {
        "dry run: validated against the table, not inserted"}


def test_missing_database_fails_the_run_without_creating_it(compiled, inbox):
    compiled["db"].unlink()
    s = ops.run_sandboxed(NAME)
    assert s["status"] in ("failed", "blocked"), s
    assert not compiled["db"].exists()


def test_value_that_does_not_fit_is_quarantined_before_any_write(workspace, db):
    from runner.loader import load_integration
    from runner.pipeline import MemoryStore, process_record
    from runner.testing import FakeHttp
    raw = sqlite_spec()
    raw["sink"]["fields"]["Date Applied"] = {"from": "record.title"}
    built = ops.compile_and_test(write_spec(workspace, raw), examples_path=JOBS / "examples.yaml")
    integ = load_integration(built["path"])
    ex = yaml.safe_load((JOBS / "examples.yaml").read_text())[0]
    http = FakeHttp()
    out = process_record(integ, ex["record"], http, MemoryStore())
    assert (out.status, out.reason) == ("quarantined", "VALIDATION_FAILED") and "Date Applied" in out.detail
    assert http.sink_writes == []


def test_control_fetch_contract_for_sqlite(workspace, db):
    doc = ops.fetch_contract(parse_spec(sqlite_spec()))
    assert "Company" in props(doc)
    with pytest.raises(RuntimeError) as e:
        ops.fetch_contract(parse_spec(sqlite_spec(table="Nope")))
    assert "SINK_CONTRACT" in str(e.value)


def test_golden_bodies_validate_against_the_contract(compiled):
    contract = json.loads((compiled["vdir"] / "contract" / "openapi.json").read_text())
    for ex in json.loads((compiled["vdir"] / "golden.json").read_text()):
        if "body" in ex["expect"]:
            assert validate_body(contract, "POST", "/rows", ex["expect"]["body"]) == []


def test_form_and_api_packages_unchanged(workspace):
    """The sqlite branch does not touch form/API code generation."""
    from compiler.compile import compile_spec, load_examples
    from spec import load_spec
    res = compile_spec(load_spec(JOBS / "spec.yaml"), version=1, out_root=workspace / "c", examples=load_examples(JOBS / "examples.yaml"))
    text = (res.path / "adapters.py").read_text()
    assert "sqlite" not in text.lower() and "airtable" not in text.lower()
