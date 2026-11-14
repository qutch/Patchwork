"""The page inbox (docs/contracts.md V1, C2): pages posted by the extension, stored in
$PATCHWORK_WORKSPACE/pages.db and served back as the `jobs_to_tracker` source (`GET /api/pages`).

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
  captured_date TEXT NOT NULL
);
"""

COLUMNS = ("seq", "url", "canonical_url", "site", "title", "html", "text", "captured_at", "captured_date")
MAX_LIMIT = 1000


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
    return {"id": page_id(d["seq"]), **{k: d[k] for k in COLUMNS}}


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
                    break
                except sqlite3.OperationalError as e:
                    if "locked" not in str(e).lower() or time.monotonic() > deadline:
                        raise
                    time.sleep(0.02)
            yield db
        finally:
            db.close()

    def add(self, url: str, title: str, html: str) -> dict[str, Any]:
        url, title, html = url or "", title or "", html or ""
        local = datetime.now().astimezone()
        row = {"url": url, "canonical_url": canonical_url(url, html), "site": site_of(url), "title": title,
               "html": html, "text": visible_text(html),
               "captured_at": local.astimezone(timezone.utc).isoformat(timespec="milliseconds"),
               "captured_date": local.date().isoformat()}
        cols = list(row)
        with self._conn() as db:
            cur = db.execute(f"INSERT INTO pages({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                             [row[c] for c in cols])
            seq = int(cur.lastrowid)
        return _record({"seq": seq, **row})

    def list(self, since: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), MAX_LIMIT))
        with self._conn() as db:
            rows = db.execute("SELECT * FROM pages WHERE seq > ? ORDER BY seq LIMIT ?", (int(since), limit)).fetchall()
        return [_record(r) for r in rows]

    def get(self, pid: str) -> dict[str, Any] | None:
        seq = seq_of(pid)
        if seq is None:
            return None
        with self._conn() as db:
            row = db.execute("SELECT * FROM pages WHERE seq = ?", (seq,)).fetchone()
        return _record(row) if row else None

    def summaries(self, limit: int | None = None, newest_first: bool = True) -> list[dict[str, Any]]:
        """Page records without `html`/`text` (cheap: the tracker polls this every second)."""
        order = "DESC" if newest_first else "ASC"
        sql = f"SELECT seq, url, canonical_url, site, title, captured_at, captured_date FROM pages ORDER BY seq {order}"
        args: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            args = (max(1, int(limit)),)
        with self._conn() as db:
            rows = db.execute(sql, args).fetchall()
        return [{"id": page_id(r["seq"]), **dict(r)} for r in rows]

    def count(self) -> int:
        with self._conn() as db:
            return int(db.execute("SELECT COUNT(*) FROM pages").fetchone()[0])

    def clear(self) -> int:
        with self._conn() as db:
            return int(db.execute("DELETE FROM pages").rowcount)
