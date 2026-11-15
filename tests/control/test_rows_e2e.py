"""G7 (contracts V17, V18): one page or file becomes many checked rows, end to end, with no model.

The real control plane (page and document inboxes, run on arrival, the learn trigger), the real compiler and the
real runner subprocess, a SQLite destination under the workspace, and the scripted agent for LEARN.

The readers (`runner.documents`) are the teammate's. Until they land a stand-in (tests/control/standin_readers.py)
reads uploads in the control plane; the runner never needs one for these tests because a document record already
carries its `tables`. Tests that need the runner itself to read an HTML table, or an .xlsx, run only against the real
module (they skip until it exists) and have not been run against it yet."""

from __future__ import annotations

import copy
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import httpx
import pytest
import yaml

from agents.scripted import ScriptedAgentService
from control import ops
from control.api import create_app
from control.documents import DocumentInbox
from control.pages import PageInbox
from runner.store import Store
from tests.conftest import _Server
from tests.control import standin_readers
from tests.control.v2_fakes import wait_idle

REPO = Path(__file__).resolve().parents[2]
TABLES = REPO / "fixtures" / "tables"
PEOPLE_CSV = (TABLES / "people.csv").read_bytes()

PEOPLE_ROWS = [{"Name": "Ada", "Team": "Core", "Age": 36, "File": "people.csv"},
               {"Name": "Grace", "Team": "Core", "Age": 45, "File": "people.csv"},
               {"Name": "Linus", "Team": "Kernel", "Age": 28, "File": "people.csv"}]
ORDER_A = [{"Ref": "R-7", "N": 1, "Item": "Bolt", "Qty": 2, "Line": 3.0},
           {"Ref": "R-7", "N": 2, "Item": "Nut", "Qty": 4, "Line": 1.0},
           {"Ref": "R-7", "N": 3, "Item": "Washer", "Qty": 10, "Line": 1.0}]
ORDER_B = [{"Ref": "R-8", "N": 1, "Item": "Lamp", "Qty": 1, "Line": 20.0},
           {"Ref": "R-8", "N": 2, "Item": "Desk", "Qty": 1, "Line": 80.0},
           {"Ref": "R-8", "N": 3, "Item": "Chair", "Qty": 2, "Line": 50.0}]
ORDER_C = [{"Ref": "R-9", "N": 1, "Item": "Hat", "Qty": 3, "Line": 15.0},
           {"Ref": "R-9", "N": 2, "Item": "Scarf", "Qty": 1, "Line": 12.5},
           {"Ref": "R-9", "N": 3, "Item": "Gloves", "Qty": 2, "Line": 10.0}]


# ---- helpers --------------------------------------------------------------------------------------------

def make_db(ws: Path, name: str, ddl: str) -> Path:
    with closing(sqlite3.connect(ws / name)) as conn, conn:
        conn.execute(ddl)
    return ws / name


def table_rows(ws: Path, name: str, table: str) -> list[dict]:
    with closing(sqlite3.connect(ws / name)) as conn:
        conn.row_factory = sqlite3.Row
        return [{k: r[k] for k in r.keys() if k != "id"} for r in conn.execute(f'SELECT * FROM "{table}" ORDER BY id')]


def deploy(ws: Path, spec: dict, record: dict, bodies: list[dict]) -> None:
    """Compile (with the real generated tests) and deploy v1 of `spec`, with one golden example."""
    (ws / "spec.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))
    (ws / "examples.yaml").write_text(yaml.safe_dump([{"name": "first_layout", "record": record,
                                                       "expect": {"rows": bodies}}]))
    built = ops.compile_and_test(ws / "spec.yaml", examples_path=ws / "examples.yaml")
    assert built["tests_passed"], built["tests_summary"]
    ops.deploy(spec["name"], int(built["version"]))


def load(name: str) -> dict:
    return yaml.safe_load((TABLES / name).read_text())


def blocks_variant(spec: dict) -> dict:
    """spec_html.yaml for the first layout in the page tests: site b.example's repeated blocks (a `css` select), so the
    runner needs no HTML reader. The `table` select of spec_html.yaml itself is covered by the test that needs the
    real readers (an HTML table's cells are one text line each, so only a reader can group them into rows)."""
    spec = copy.deepcopy(spec)
    parser = spec["extract"]["parsers"][0]
    parser.update(id="shop_b", match={"site": "b.example"})
    parser["rows"] = {"select": {"css": "div.line"},
                      "fields": {"item": {"css": ".name"}, "qty": {"css": ".n", "cast": "int"},
                                 "line": {"css": ".amt", "clean": "money"}}}
    return spec


@pytest.fixture(params=["standin", "real"])
def readers(request, monkeypatch):
    if request.param == "real":
        pytest.importorskip("runner.documents")  # the teammate's readers (Task 2)
        return "real"
    standin_readers.install(monkeypatch)
    return "standin"


@pytest.fixture
def control(workspace, monkeypatch):
    """The real control plane on a free port; yields the httpx client (`.app` is the FastAPI app)."""
    for var in ("PATCHWORK_CONTROL_URL", "PATCHWORK_EXTENSION_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PATCHWORK_RUN_PACE_MS", "0")

    def start():
        app = create_app(agent=ScriptedAgentService(pace_s=0), dotenv=False)
        srv = _Server(app)
        client = srv.__enter__()
        http = httpx.Client(base_url=client.url, timeout=60)
        http.app = app  # type: ignore[attr-defined]
        return srv, http, app

    handles = []

    def make():
        srv, http, app = start()
        handles.append((srv, http, app))
        return http

    yield make
    for srv, http, app in handles:
        app.state.runs.stop_all()
        http.close()
        srv.__exit__(None, None, None)


def token(api: httpx.Client) -> dict:
    return {"X-Patchwork-Token": api.get("/api/extension").json()["token"]}


def upload(api: httpx.Client, name: str, data: bytes, integration: str) -> httpx.Response:
    return api.post("/api/documents", files={"file": (name, data, "text/csv")}, data={"integration": integration},
                    headers=token(api))


def post_page(api: httpx.Client, url: str, html: str, integration: str) -> httpx.Response:
    return api.post("/api/pages", json={"url": url, "title": "t", "html": html, "integration": integration},
                    headers=token(api))


def last_run(name: str) -> dict:
    return Store().runs(name, limit=1)[0]


def outcomes(name: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    store = Store()
    for run in reversed(store.runs(name, limit=100)):
        for r in store.records(run["id"]):
            out.setdefault(r["record_id"], []).append({"status": r["status"], "reason": r["reason"], "run": run["id"]})
    return out


def wait_rows(ws: Path, db: str, table: str, n: int, timeout: float = 90) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(table_rows(ws, db, table)) >= n:
            return
        time.sleep(0.1)
    raise AssertionError(f"{table} has {len(table_rows(ws, db, table))} rows, wanted {n}")


# ---- a sheet becomes one row per data row ----------------------------------------------------------------

PEOPLE_DDL = "CREATE TABLE People (id INTEGER PRIMARY KEY, Name TEXT NOT NULL, Team TEXT, Age INTEGER, File TEXT)"


@pytest.fixture
def people(workspace, readers, control, monkeypatch):
    monkeypatch.setenv("PATCHWORK_AI_ENABLED", "0")  # held documents stay held: no LEARN in this leg
    make_db(workspace, "people.db", PEOPLE_DDL)
    scratch = DocumentInbox(workspace / "scratch" / "documents.db")
    first = scratch.add_bytes(PEOPLE_CSV, "people.csv", "text/csv", "people_rows")
    deploy(workspace, load("spec_csv.yaml"), scratch.get(first["leaves"][0]), PEOPLE_ROWS)
    return control()


def test_csv_upload_writes_one_row_per_data_row_and_a_reupload_writes_nothing(workspace, people):
    r = upload(people, "people.csv", PEOPLE_CSV, "people_rows")
    assert r.status_code == 201 and len(r.json()["leaves"]) == 1, r.text
    wait_idle(people, "people_rows")
    assert table_rows(workspace, "people.db", "People") == PEOPLE_ROWS
    run = last_run("people_rows")
    assert run["status"] == "completed" and run["counts"] == {"success": 3}
    again = upload(people, "people.csv", PEOPLE_CSV, "people_rows")  # the same bytes: the same document
    assert again.status_code == 200 and again.json()["duplicate"] is True and again.json()["id"] == r.json()["id"]
    wait_idle(people, "people_rows")
    assert len(Store().runs("people_rows", limit=10)) == 1 and len(table_rows(workspace, "people.db", "People")) == 3
    reordered = b"Name,Team,Age\nGrace,Core,45\nAda,Core,36\nLinus,Kernel,28\n"  # a new file with the same rows
    assert upload(people, "people.csv", reordered, "people_rows").status_code == 201
    wait_idle(people, "people_rows")
    assert last_run("people_rows")["counts"] == {"duplicate": 3}
    assert table_rows(workspace, "people.db", "People") == PEOPLE_ROWS  # nothing written twice


@pytest.mark.parametrize("data,reason,needle", [
    (b"Name,Age\nAda,36\n", "PARSE_NO_MATCH", "Team"),                       # a column is missing
    (b"Name,Team,Age\nAda,Core,36\nBob,Ops,0\n", "VALIDATION_FAILED", "row 2"),  # one bad row holds the whole file
])
def test_a_file_that_does_not_fit_is_held_and_nothing_is_written(workspace, people, data, reason, needle):
    assert upload(people, "bad.csv", data, "people_rows").status_code == 201
    wait_idle(people, "people_rows")
    assert table_rows(workspace, "people.db", "People") == []
    held = outcomes("people_rows")["doc-000001"]
    assert held[-1]["status"] == "quarantined" and held[-1]["reason"] == reason
    detail = Store().records(last_run("people_rows")["id"])[0]["detail"]
    assert needle in detail


def test_xlsx_upload_writes_the_same_rows(workspace, people, readers):
    pytest.importorskip("openpyxl")
    if readers != "real":
        pytest.skip("reading .xlsx needs the real runner.documents")
    from fixtures.tables.make import write_xlsx

    path = write_xlsx(workspace / "people.xlsx")
    r = people.post("/api/documents", files={"file": ("people.xlsx", path.read_bytes(), "application/vnd.ms-excel")},
                    data={"integration": "people_rows"}, headers=token(people))
    assert r.status_code == 201
    wait_idle(people, "people_rows")
    assert table_rows(workspace, "people.db", "People") == PEOPLE_ROWS


# ---- pages: a held layout is learned once, then its rows are written ------------------------------------

LINES_DDL = "CREATE TABLE Lines (id INTEGER PRIMARY KEY, Ref TEXT, N INTEGER, Item TEXT, Qty INTEGER, Line REAL)"
A_URL, B_URL, C_URL = "https://a.example/orders/7", "https://b.example/orders/8", "https://c.example/orders/9"


def html_of(name: str) -> str:
    return (TABLES / name).read_text()


def page_stack(workspace, control, spec: dict, first_url: str, first_file: str, bodies: list[dict]) -> httpx.Client:
    make_db(workspace, "lines.db", LINES_DDL)
    record = PageInbox(workspace / "scratch" / "pages.db").add(first_url, "t", html_of(first_file))
    deploy(workspace, spec, record, bodies)
    return control()


def test_a_new_layout_is_learned_once_and_its_rows_are_written(workspace, control):
    api = page_stack(workspace, control, blocks_variant(load("spec_html.yaml")), B_URL, "orders-b.html", ORDER_B)
    assert post_page(api, B_URL, html_of("orders-b.html"), "order_lines").status_code == 201
    wait_idle(api, "order_lines")
    assert table_rows(workspace, "lines.db", "Lines") == ORDER_B
    assert last_run("order_lines")["counts"] == {"success": 3}

    # site C lists its rows differently: no parser reads it, so the page is held and the scripted agent learns it
    c = post_page(api, C_URL, html_of("orders-c.html"), "order_lines")
    assert c.status_code == 201
    wait_rows(workspace, "lines.db", "Lines", 6)
    wait_idle(api, "order_lines")
    assert table_rows(workspace, "lines.db", "Lines") == ORDER_B + ORDER_C  # exactly B's rows plus C's, none twice
    assert Store().active_version("order_lines") == 2
    spec = yaml.safe_load((workspace / "integrations" / "order_lines" / "v2" / "spec.yaml").read_text())
    learned = next(p for p in spec["extract"]["parsers"] if p["match"] == {"site": "c.example"})
    assert learned["origin"]["kind"] == "learned" and learned["origin"]["approved_by"] == "auto"
    assert list(learned["rows"]["select"]) == ["lines"]
    seen = outcomes("order_lines")
    assert seen[c.json()["id"]][0]["reason"] == "PARSE_NO_MATCH"  # held first
    assert [seen[f"{c.json()['id']}#{n}"][-1]["status"] for n in (1, 2, 3)] == ["success"] * 3  # then written

    # the same pages again write nothing new
    for url, file in ((B_URL, "orders-b.html"), (C_URL, "orders-c.html")):
        assert post_page(api, url, html_of(file), "order_lines").status_code == 201
    wait_idle(api, "order_lines")
    assert len(table_rows(workspace, "lines.db", "Lines")) == 6


def test_a_page_whose_total_does_not_add_up_is_held_and_writes_nothing(workspace, control, monkeypatch):
    monkeypatch.setenv("PATCHWORK_AI_ENABLED", "0")
    api = page_stack(workspace, control, blocks_variant(load("spec_html.yaml")), B_URL, "orders-b.html", ORDER_B)
    bad = html_of("orders-b.html").replace("Sum $150.00", "Sum $160.00")
    assert bad != html_of("orders-b.html")
    assert post_page(api, B_URL, bad, "order_lines").status_code == 201
    wait_idle(api, "order_lines")
    assert table_rows(workspace, "lines.db", "Lines") == []
    held = outcomes("order_lines")["pg-000001"][-1]
    assert held["status"] == "quarantined" and held["reason"] == "VALIDATION_FAILED"
    detail = Store().records(last_run("order_lines")["id"])[0]["detail"]
    assert "sum_equals" in detail and "150.00" in detail and "160.00" in detail


def test_an_html_table_page_needs_the_real_reader(workspace, control):
    """spec_html.yaml as written (a `table` select over the page's HTML): the runner reads the table itself, so this
    needs the real runner.documents.read_html. Not run against it yet."""
    pytest.importorskip("runner.documents")
    api = page_stack(workspace, control, load("spec_html.yaml"), A_URL, "orders-a.html", ORDER_A)
    assert post_page(api, A_URL, html_of("orders-a.html"), "order_lines").status_code == 201
    wait_idle(api, "order_lines")
    assert table_rows(workspace, "lines.db", "Lines") == ORDER_A
