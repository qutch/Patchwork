"""The page inbox (docs/contracts.md V1, C2, V15): pages posted by the extension, stored in
$PATCHWORK_WORKSPACE/pages.db and served back as the page integrations' source (`GET /api/pages`).

A page goes to one integration (V15): `integration` is the name the extension sent, or NULL (untagged:
the default page integration's). `list`/`summaries`/`clear` take `integration=` (+ `include_untagged`) to
see one integration's pages; without it they see every page. Existing pages.db files gain the column on
first open (nullable, so old pages are untagged).

Records are built once, at ingest, with the same helpers the runner and the agents use
(`runner.html`), so every reader sees the same `site`, `canonical_url` and `text`. Never logs HTML.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from runner.html import canonical_url, site_of, visible_text
from runner.store import workspace_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT NOT NULL,
  canonical_url TEXT NOT NULL,
  site TEXT NOT NULL,
  title TEXT NOT NULL,
  html TEXT NOT NULL,
  text TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  captured_date TEXT NOT NULL,
  integration TEXT
);
CREATE TABLE IF NOT EXISTS learns (
  page_id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL,
  site TEXT NOT NULL,
  integration TEXT NOT NULL,
  started_at TEXT NOT NULL
);
"""

COLUMNS = ("seq", "url", "canonical_url", "site", "title", "html", "text", "captured_at", "captured_date",
           "integration")
SUMMARY_COLUMNS = ("seq", "url", "canonical_url", "site", "title", "captured_at", "captured_date", "integration")
MAX_LIMIT = 1000
_MIGRATED: set[str] = set()


def _migrate(db: sqlite3.Connection, path: Path) -> None:
    """V15: pages.db files from before page->integration routing get a nullable `integration` column."""
    key = str(path)
    if key in _MIGRATED:
        return
    cols = {r[1] for r in db.execute("PRAGMA table_info(pages)")}
    if "integration" not in cols:
        try:
            db.execute("ALTER TABLE pages ADD COLUMN integration TEXT")
        except sqlite3.OperationalError as e:  # another connection added it first
            if "duplicate column" not in str(e).lower():
                raise
    _MIGRATED.add(key)


def _where(integration: str | None, include_untagged: bool) -> tuple[str, tuple]:
    """SQL condition (and args) for one integration's pages; every page when `integration` is None."""
    if integration is None:
        return "1=1", ()
    if include_untagged:
        return "(integration = ? OR integration IS NULL)", (integration,)
    return "integration = ?", (integration,)


def page_id(seq: int) -> str:
    return f"pg-{seq:06d}"


def seq_of(pid: str) -> int | None:
    if not isinstance(pid, str) or not pid.startswith("pg-"):
        return None
    try:
        return int(pid[3:])
    except ValueError:
        return None


def _record(row: sqlite3.Row | dict) -> dict[str, Any]:
    d = dict(row)
    return {"id": page_id(d["seq"]), **{k: d.get(k) for k in COLUMNS}}


class PageInbox:
    """SQLite-backed, safe across threads and processes: each call opens its own connection, and
    `seq` comes from AUTOINCREMENT inside one write transaction, so concurrent adds never collide.
    `clear()` keeps the counter, so a page id is never reused within a workspace."""

    def __init__(self, path: str | Path | None = None):
        self._path = Path(path) if path else None
        with self._conn() as db:
            db.executescript(SCHEMA)

    @property
    def path(self) -> Path:
        return self._path or workspace_dir() / "pages.db"

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, isolation_level=None, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            deadline = time.monotonic() + 15
            while True:  # first open of a fresh DB can return SQLITE_BUSY immediately
                try:
                    db.execute("PRAGMA journal_mode=WAL")
                    db.executescript(SCHEMA)
                    _migrate(db, path)
                    break
                except sqlite3.OperationalError as e:
                    if "locked" not in str(e).lower() or time.monotonic() > deadline:
                        raise
                    time.sleep(0.02)
            yield db
        finally:
            db.close()

    def add(self, url: str, title: str, html: str, integration: str | None = None) -> dict[str, Any]:
        url, title, html = url or "", title or "", html or ""
        local = datetime.now().astimezone()
        row = {"url": url, "canonical_url": canonical_url(url, html), "site": site_of(url), "title": title,
               "html": html, "text": visible_text(html),
               "captured_at": local.astimezone(timezone.utc).isoformat(timespec="milliseconds"),
               "captured_date": local.date().isoformat(), "integration": integration or None}
        cols = list(row)
        with self._conn() as db:
            cur = db.execute(f"INSERT INTO pages({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                             [row[c] for c in cols])
            seq = int(cur.lastrowid)
        return _record({"seq": seq, **row})

    def list(self, since: int = 0, limit: int = 100, integration: str | None = None,
             include_untagged: bool = True) -> list[dict[str, Any]]:
        """Pages after `since`, ascending. With `integration`: that integration's pages (plus untagged ones
        when `include_untagged`, i.e. for the default page integration)."""
        limit = max(1, min(int(limit), MAX_LIMIT))
        cond, args = _where(integration, include_untagged)
        with self._conn() as db:
            rows = db.execute(f"SELECT * FROM pages WHERE seq > ? AND {cond} ORDER BY seq LIMIT ?",
                              (int(since), *args, limit)).fetchall()
        return [_record(r) for r in rows]

    def get(self, pid: str) -> dict[str, Any] | None:
        seq = seq_of(pid)
        if seq is None:
            return None
        with self._conn() as db:
            row = db.execute("SELECT * FROM pages WHERE seq = ?", (seq,)).fetchone()
        return _record(row) if row else None

    def summaries(self, limit: int | None = None, newest_first: bool = True, integration: str | None = None,
                  include_untagged: bool = True) -> list[dict[str, Any]]:
        """Page records without `html`/`text` (cheap: the tracker polls this every second)."""
        order = "DESC" if newest_first else "ASC"
        cond, args = _where(integration, include_untagged)
        sql = f"SELECT {', '.join(SUMMARY_COLUMNS)} FROM pages WHERE {cond} ORDER BY seq {order}"
        if limit is not None:
            sql += " LIMIT ?"
            args = (*args, max(1, int(limit)))
        with self._conn() as db:
            rows = db.execute(sql, args).fetchall()
        return [{"id": page_id(r["seq"]), **dict(r)} for r in rows]

    def count(self) -> int:
        with self._conn() as db:
            return int(db.execute("SELECT COUNT(*) FROM pages").fetchone()[0])

    def clear(self, integration: str | None = None, include_untagged: bool = True) -> int:
        """Delete pages and their learn-thread links; returns the number of pages removed. Without
        `integration` every page; with it only that integration's (V15: a demo reset of one integration
        leaves the others' pages alone)."""
        cond, args = _where(integration, include_untagged)
        with self._conn() as db:
            if integration is None:
                n = int(db.execute("DELETE FROM pages").rowcount)
                db.execute("DELETE FROM learns")
                return n
            ids = [page_id(r[0]) for r in db.execute(f"SELECT seq FROM pages WHERE {cond}", args)]
            n = int(db.execute(f"DELETE FROM pages WHERE {cond}", args).rowcount)
            db.execute("DELETE FROM learns WHERE integration = ?", (integration,))
            for chunk in range(0, len(ids), 500):
                part = ids[chunk:chunk + 500]
                db.execute(f"DELETE FROM learns WHERE page_id IN ({','.join('?' * len(part))})", part)
            return n

    # -- learn threads started for pages (V9: which page a LEARN thread belongs to) ----------
    def add_learn(self, page_id: str, thread_id: str, site: str, integration: str) -> None:
        with self._conn() as db:
            db.execute("INSERT OR REPLACE INTO learns VALUES (?,?,?,?,?)",
                       (page_id, thread_id, site, integration,
                        datetime.now(timezone.utc).isoformat(timespec="milliseconds")))

    def learns(self, integration: str | None = None) -> dict[str, dict[str, Any]]:
        """{page_id: {page_id, thread_id, site, integration, started_at}}."""
        sql, args = "SELECT * FROM learns", ()
        if integration:
            sql, args = sql + " WHERE integration = ?", (integration,)
        with self._conn() as db:
            return {r["page_id"]: dict(r) for r in db.execute(sql, args).fetchall()}
