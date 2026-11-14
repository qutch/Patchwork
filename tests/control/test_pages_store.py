"""W4a-1: the page inbox store (docs/contracts.md V1)."""

import threading
from datetime import datetime
from pathlib import Path

from control.pages import PageInbox
from runner.html import visible_text

PAGES = Path(__file__).resolve().parents[2] / "fixtures" / "pages"
V1_KEYS = {"id", "seq", "url", "canonical_url", "site", "title", "html", "text", "captured_at", "captured_date",
           "integration"}  # V15: the integration the page was sent to (None = untagged)


def test_add_builds_v1_record(workspace):
    inbox = PageInbox()
    html = (PAGES / "lever-1.html").read_text()
    url = "https://www.Jobs.Lever.co/palantir/0ccbe620-a3ef-41d1-a5c4-68e56b3c91d0?lever-source=x&utm_medium=y#apply"
    rec = inbox.add(url, "Palantir - American Tech Fellowship", html)
    assert set(rec) == V1_KEYS
    assert rec["id"] == "pg-000001" and rec["seq"] == 1
    assert rec["site"] == "jobs.lever.co"
    assert "lever-source" not in rec["canonical_url"] and "utm_" not in rec["canonical_url"]
    assert "#" not in rec["canonical_url"] and rec["canonical_url"].endswith("lever.co/palantir/0ccbe620-a3ef-41d1-a5c4-68e56b3c91d0")
    assert rec["url"] == url and rec["html"] == html and rec["title"].startswith("Palantir")
    assert rec["text"] == visible_text(html) and "American Tech Fellowship" in rec["text"]
    captured = datetime.fromisoformat(rec["captured_at"])
    assert captured.utcoffset() is not None  # timezone-aware
    assert rec["captured_date"] == captured.astimezone().date().isoformat()  # local date (C7)
    assert (workspace / "pages.db").exists()
    assert inbox.get("pg-000001") == rec
    assert inbox.get("pg-000099") is None and inbox.get("nonsense") is None


def test_list_since_is_exclusive_and_ordered(workspace):
    inbox = PageInbox()
    for i in range(5):
        inbox.add(f"https://example.org/job/{i}", f"Job {i}", f"<html><body><h1>Job {i}</h1></body></html>")
    assert [p["seq"] for p in inbox.list()] == [1, 2, 3, 4, 5]
    assert [p["seq"] for p in inbox.list(since=2)] == [3, 4, 5]
    assert [p["seq"] for p in inbox.list(since=2, limit=2)] == [3, 4]
    assert inbox.list(since=5) == []
    assert inbox.list()[0]["id"] == "pg-000001"


def test_clear(workspace):
    inbox = PageInbox()
    for i in range(3):
        inbox.add(f"https://example.org/{i}", "t", "<p>x</p>")
    assert inbox.clear() == 3
    assert inbox.list() == []
    # ids are never reused after a clear, so old runs/threads can't be confused with new pages
    assert inbox.add("https://example.org/new", "t", "<p>y</p>")["seq"] == 4


def test_concurrent_adds_get_unique_seq(workspace):
    inbox = PageInbox()
    out, errors = [], []

    def add(i: int) -> None:
        try:
            # a fresh instance per thread too: separate connections must not collide either
            out.append((inbox if i % 2 else PageInbox()).add(f"https://example.org/{i}", "t", "<p>x</p>")["seq"])
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=add, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sorted(out) == list(range(1, 11))
    assert [p["seq"] for p in inbox.list()] == list(range(1, 11))


def test_bad_html_never_raises(workspace):
    rec = PageInbox().add("not a url", "", "<<<")
    assert rec["site"] == "" and rec["seq"] == 1
