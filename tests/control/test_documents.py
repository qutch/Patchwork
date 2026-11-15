"""G4 (contracts V17): the document inbox, the adapter over runner.documents, the upload API."""

from __future__ import annotations

import csv
import io
import sys
import types
from dataclasses import dataclass, field
from typing import Any

import pytest

from control.autorun import is_document_spec, is_page_spec
from control.documents import DocumentInbox, SENDER_KEY, sender_domain, to_records
from control.inboxes import inbox_for
from control.pages import PageInbox
from control.page_hooks import PageHooks
from tests.control.v2_fakes import (NAME, make_page_integration,  # noqa: F401
                                    page_spec, v2agent, v2api, fake_runner)

DOC_NAME = "doc_int"


@dataclass
class Table:
    header: list[str]
    rows: list[list[str]]
    where: str = ""


@dataclass
class Document:
    kind: str
    name: str
    text: str = ""
    html: str | None = None
    tables: list[Table] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    children: list["Document"] = field(default_factory=list)


def fake_read_any(data: bytes, name: str, content_type: str = "") -> Document:
    """Stands in for the teammate's runner.documents.read_any, so these tests cover the control plane only."""
    if name.endswith(".csv"):
        rows = list(csv.reader(io.StringIO(data.decode())))
        return Document(kind="sheet", name=name, tables=[Table(rows[0], rows[1:], "sheet 1")])
    if name.endswith(".eml"):
        pdf = Document(kind="pdf", name="a.pdf", meta={"no_text_layer": "true"})
        return Document(kind="email", name=name, text="see attached",
                        meta={SENDER_KEY: "b@north.example", "subject": "S"}, children=[pdf])
    raise ValueError("not a readable document")


@pytest.fixture
def readers(monkeypatch):
    mod = types.ModuleType("runner.documents")
    mod.read_any, mod.Document, mod.Table = fake_read_any, Document, Table  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "runner.documents", mod)


def doc_spec(name: str = DOC_NAME) -> dict:
    spec = page_spec([{"id": "p", "match": {"site": "upload"}, "fields": {"title": {"regex": "(x)"}}}], name)
    spec["source"]["list"]["path"] = "/api/documents"
    return spec


@pytest.fixture
def doc_integration(workspace):
    make_page_integration(name=DOC_NAME, spec=doc_spec())
    return DOC_NAME


@pytest.fixture
def auto_spy(v2api):
    class Spy:
        requests: list[str] = []

    spy = Spy()
    spy.requests = []

    def request(name, *a, **k):
        spy.requests.append(name)
        return {"run_id": None, "queued": False}

    v2api.app.state.auto.request = request  # type: ignore[attr-defined]
    return spy


def token(api) -> dict:
    return {"X-Patchwork-Token": api.get("/api/extension").json()["token"]}


def upload(api, name: str, data: bytes, integration: str, content_type: str = "text/csv"):
    return api.post("/api/documents", files={"file": (name, data, content_type)}, data={"integration": integration},
                    headers=token(api))


# ---- the adapter ----------------------------------------------------------------------------------------

def test_adapter_marks_leaves_site_and_flags():
    pdf = Document(kind="pdf", name="a.pdf", meta={"no_text_layer": "true"})
    mail = Document(kind="email", name="m", text="see attached",
                    meta={SENDER_KEY: "Billing <b@north.example>", "subject": "S"}, children=[pdf])
    parent, child = to_records(mail, integration="x", received_at="2026-11-14T00:00:00+00:00", sha256="h")
    assert (parent["is_leaf"], child["is_leaf"]) == (False, True)
    assert parent["site"] == child["site"] == "north.example"
    assert child["flags"] == ["no_text_layer"] and child["meta"]["subject"] == "S"
    assert child["parent_index"] == 0 and parent["parent_index"] is None
    assert child["received_date"] == "2026-11-14" and child["html"] == ""


def test_adapter_standalone_file_is_its_own_leaf_and_site_is_upload():
    sheet = Document(kind="sheet", name="p.csv", tables=[Table(["Name"], [["Ada"]], "sheet 1")])
    (only,) = to_records(sheet, integration=None, received_at="2026-11-14T00:00:00+00:00", sha256="h")
    assert only["is_leaf"] is True and only["site"] == "upload"
    assert only["tables"] == [{"header": ["Name"], "rows": [["Ada"]], "where": "sheet 1"}]


def test_adapter_email_with_body_only_is_a_leaf_and_encrypted_flag():
    mail = Document(kind="email", name="m", text="hi", meta={SENDER_KEY: "a@x.example", "encrypted": "true"})
    (only,) = to_records(mail, integration="i", received_at="2026-11-14T00:00:00+00:00", sha256="h")
    assert only["is_leaf"] is True and only["flags"] == ["encrypted"]


def test_sender_domain_forms():
    assert sender_domain({SENDER_KEY: "Ann <A@Mail.Example.COM>"}) == "mail.example.com"
    assert sender_domain({SENDER_KEY: "plain@x.org"}) == "x.org"
    assert sender_domain({}) is None and sender_domain({SENDER_KEY: "no-at-sign"}) is None


# ---- the inbox ------------------------------------------------------------------------------------------

def test_inbox_stores_email_parent_and_leaf_ids(workspace, readers):
    box = DocumentInbox()
    parent = box.add_bytes(b"raw-eml", "m.eml", "message/rfc822", "doc_int")
    assert parent["id"] == "doc-000001" and parent["leaves"] == ["doc-000002"] and parent["duplicate"] is False
    leaf = box.get("doc-000002")
    assert leaf["parent_id"] == "doc-000001" and leaf["site"] == "north.example" and leaf["flags"] == ["no_text_layer"]
    assert [d["id"] for d in box.list(integration="doc_int")] == ["doc-000002"]
    assert [d["id"] for d in box.list(integration="doc_int", leaves_only=False)] == ["doc-000001", "doc-000002"]
    assert [d["id"] for d in box.list(since=2, integration="doc_int")] == []
    assert box.list(integration="other") == []
    assert [s["id"] for s in box.summaries(integration="doc_int")] == ["doc-000002"]
    assert "text" not in box.summaries()[0]
    assert any(p.name.endswith("m.eml") for p in box.originals_dir.iterdir())


def test_inbox_duplicate_same_bytes_per_integration(workspace, readers):
    box = DocumentInbox()
    a = box.add_bytes(b"Name\nAda\n", "p.csv", "text/csv", "i1")
    b = box.add_bytes(b"Name\nAda\n", "p.csv", "text/csv", "i1")
    c = box.add_bytes(b"Name\nAda\n", "p.csv", "text/csv", "i2")
    assert a["id"] == b["id"] and b["duplicate"] is True and b["leaves"] == a["leaves"]
    assert c["id"] != a["id"] and c["duplicate"] is False
    assert box.count() == 2


def test_inbox_unreadable_is_kept_flagged_with_both_error_keys(workspace, readers):
    d = DocumentInbox().add_bytes(b"not a pdf", "x.pdf", "application/pdf", "i")
    assert d["flags"] == ["unreadable"] and d["meta"]["problem"] and d["meta"]["error"] == d["meta"]["problem"]
    assert d["leaves"] == [d["id"]]


def test_inbox_site_and_meta_overrides_and_clear_keeps_counter(workspace, readers):
    box = DocumentInbox()
    d = box.add_bytes(b"Name\nAda\n", "p.csv", "text/csv", "i", site="sqlite", meta={"table": "people"})
    assert d["site"] == "sqlite" and d["meta"]["table"] == "people"
    assert box.clear("i") == 1
    again = box.add_bytes(b"Name\nBo\n", "q.csv", "text/csv", "i")
    assert again["id"] == "doc-000002"  # an id is never reused


def test_inbox_for_dispatches_by_prefix(workspace):
    assert isinstance(inbox_for("pg-000001"), PageInbox) and isinstance(inbox_for("doc-000001"), DocumentInbox)


# ---- the API --------------------------------------------------------------------------------------------

def test_csv_upload_lists_as_runner_source_and_starts_a_run(v2api, readers, doc_integration, auto_spy):
    r = upload(v2api, "p.csv", b"Name\nAda\n", doc_integration)
    assert r.status_code == 201, r.text
    assert "text" not in r.json() and r.json()["integration"] == doc_integration
    listed = v2api.get(f"/api/documents?since=0&integration={doc_integration}").json()
    assert [d["id"] for d in listed] == r.json()["leaves"]
    assert listed[0]["tables"][0]["header"] == ["Name"] and listed[0]["site"] == "upload"
    assert auto_spy.requests == [doc_integration]
    assert v2api.get(f"/api/documents/{listed[0]['id']}").json()["tables"][0]["rows"] == [["Ada"]]
    assert v2api.get("/api/documents/doc-000099").status_code == 404
    assert [d["id"] for d in v2api.get(f"/api/documents?since={listed[0]['seq']}").json()] == []


def test_same_file_twice_is_one_document_and_starts_no_second_run(v2api, readers, doc_integration, auto_spy):
    a = upload(v2api, "p.csv", b"Name\nAda\n", doc_integration)
    b = upload(v2api, "p.csv", b"Name\nAda\n", doc_integration)
    assert (a.status_code, b.status_code) == (201, 200)
    assert a.json()["id"] == b.json()["id"] and b.json()["duplicate"] is True
    assert auto_spy.requests == [doc_integration]


def test_unreadable_upload_is_kept_and_flagged(v2api, readers, doc_integration, auto_spy):
    d = upload(v2api, "x.pdf", b"not a pdf", doc_integration, "application/pdf").json()
    assert d["flags"] == ["unreadable"] and d["meta"]["problem"]


def test_upload_needs_token_and_a_document_integration_and_a_file(v2api, readers, doc_integration, auto_spy):
    body = {"files": {"file": ("p.csv", b"Name\nAda\n")}, "data": {"integration": doc_integration}}
    assert v2api.post("/api/documents", **body).status_code == 401
    assert v2api.post("/api/documents", **body, headers={"X-Patchwork-Token": "wrong"}).status_code == 401
    r = upload(v2api, "p.csv", b"Name\nAda\n", NAME)  # a page integration is not a document integration
    assert r.status_code == 422 and "not a document integration" in r.json()["detail"]
    assert upload(v2api, "p.csv", b"Name\nAda\n", "").status_code == 422
    assert v2api.post("/api/documents", data={"integration": doc_integration}, headers=token(v2api)).status_code == 422
    assert v2api.post("/api/documents", content=b"x", headers={**token(v2api), "Content-Type": "application/json"}
                      ).status_code == 422
    assert v2api.get("/api/documents").json() == []  # nothing stored
    assert auto_spy.requests == []


def test_upload_without_readers_is_503_not_a_lost_file(v2api, doc_integration, auto_spy, monkeypatch):
    monkeypatch.setitem(sys.modules, "runner.documents", None)  # import raises ImportError
    r = upload(v2api, "p.csv", b"Name\nAda\n", doc_integration)
    assert r.status_code == 503 and "readers" in r.json()["detail"]


def test_wizard_draft_waits_and_starts_nothing(v2api, readers, doc_integration, auto_spy, monkeypatch):
    from control import drafts

    monkeypatch.setattr(drafts, "waiting_names", lambda: ["draft_docs"])
    r = upload(v2api, "p.csv", b"Name\nAda\n", "draft_docs")
    assert r.status_code == 201 and r.json()["run_id"] is None
    assert auto_spy.requests == []


def test_stream_emits_document_events(v2api, readers, doc_integration, auto_spy):
    from tests.control.conftest import read_sse

    upload(v2api, "p.csv", b"Name\nAda\n", doc_integration)
    events = read_sse(v2api, f"/api/documents/stream?integration={doc_integration}", until=lambda e: True, timeout=10)
    assert events[0]["event"] == "document" and events[0]["data"]["id"] == "doc-000001"
    assert "text" not in events[0]["data"] and "tables" not in events[0]["data"]


# ---- specs and hooks ------------------------------------------------------------------------------------

def test_page_and_document_specs_are_told_apart():
    pages, docs = page_spec([]), doc_spec()
    assert is_page_spec(pages) and not is_document_spec(pages)
    assert is_document_spec(docs) and not is_page_spec(docs)
    assert not is_page_spec(None) and not is_document_spec({})


class _Agent:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def start_learn(self, pid, name):
        self.calls.append((pid, name))
        return f"learn-{pid}"

    def status(self, tid):
        return {"state": "done"}


class _Auto:
    def request(self, *a, **k):
        return {}


def test_flagged_document_never_learned(workspace, readers, doc_integration):
    box, agent = DocumentInbox(), _Agent()
    flagged = box.add_bytes(b"raw-eml", "m.eml", "message/rfc822", doc_integration)["leaves"][0]
    clean = box.add_bytes(b"Name\nAda\n", "p.csv", "text/csv", doc_integration)["leaves"][0]
    hooks = PageHooks(PageInbox(), _Auto(), lambda: True, lambda: agent, box)
    assert hooks.maybe_learn(doc_integration, [flagged]) == [] and agent.calls == []
    assert hooks.maybe_learn(doc_integration, [clean]) == [f"learn-{clean}"]  # the control: unflagged is learned
    assert box.learns(doc_integration)[clean]["thread_id"] == f"learn-{clean}"
    assert hooks.maybe_learn(doc_integration, [clean]) == []  # and at most once
