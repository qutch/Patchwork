"""The document inbox (docs/contracts.md V17): files and emails turned into records by `runner.documents`
(the readers) and served back as a document integration's source (`GET /api/documents`).

A *document* is one record: `{id, seq, parent_id, kind, integration, site, name, text, html, tables, meta, flags,
sha256, received_at, received_date, is_leaf}`. An email is a parent with its attachments as children. Only
*leaves* (a child pdf/sheet/html/text, or a parent with no such child) are served to the runner, so the parent
email is kept for audit but never written as a row.

The inbox is domain-free: it never knows what a document is about. Field names and meaning come from the spec.
Bodies (`text`, `html`) are never logged. Originals are kept under `$PATCHWORK_WORKSPACE/documents/`.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Iterator

from runner.store import workspace_dir

SENDER_KEY = "from"  # runner.documents puts the bare, lower-cased sender address here (teammate Task 2)
LEAF_KINDS = ("pdf", "sheet", "html", "text")
MAX_LIMIT = 1000

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_seq INTEGER,
  kind TEXT NOT NULL,
  integration TEXT,
  site TEXT NOT NULL,
  name TEXT NOT NULL,
  text TEXT NOT NULL,
  html TEXT NOT NULL,
  tables TEXT NOT NULL,
  meta TEXT NOT NULL,
  flags TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  received_at TEXT NOT NULL,
  received_date TEXT NOT NULL,
  is_leaf INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_sha ON documents(integration, sha256, parent_seq);
CREATE TABLE IF NOT EXISTS learns (
  page_id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL,
  site TEXT NOT NULL,
  integration TEXT NOT NULL,
  started_at TEXT NOT NULL
);
"""

COLUMNS = ("seq", "parent_seq", "kind", "integration", "site", "name", "text", "html", "tables", "meta", "flags",
           "sha256", "received_at", "received_date", "is_leaf")
SUMMARY_COLUMNS = ("seq", "parent_seq", "kind", "integration", "site", "name", "flags", "sha256", "received_at",
                   "received_date", "is_leaf")
_JSON = ("tables", "meta", "flags")


def doc_id(seq: int) -> str:
    return f"doc-{seq:06d}"


def seq_of(did: str) -> int | None:
    if not isinstance(did, str) or not did.startswith("doc-"):
        return None
    try:
        return int(did[4:])
    except ValueError:
        return None


def _record(row: sqlite3.Row | dict) -> dict[str, Any]:
    d = dict(row)
    out: dict[str, Any] = {"id": doc_id(d["seq"])}
    for k in COLUMNS:
        if k not in d:
            continue
        v = d[k]
        if k in _JSON and isinstance(v, str):
            v = json.loads(v)
        if k == "is_leaf":
            v = bool(v)
        out[k] = v
    out["parent_id"] = doc_id(d["parent_seq"]) if d.get("parent_seq") else None
    out.pop("parent_seq", None)
    return out


# ---- the adapter: Document -> V17 records ------------------------------------------------------------

def sender_domain(meta: dict[str, Any]) -> str | None:
    """The domain of the sender address in `meta` (bare or `Name <addr>`), lower-cased, else None."""
    raw = meta.get(SENDER_KEY)
    if not isinstance(raw, str):
        return None
    addr = parseaddr(raw)[1] or raw.strip()
    return addr.rsplit("@", 1)[1].strip().lower() if "@" in addr else None


def _table_dict(t: Any) -> dict[str, Any]:
    if isinstance(t, dict):
        get = t.get
    else:
        def get(k: str, default: Any = None) -> Any:
            return getattr(t, k, default)
    return {"header": [str(c) for c in (get("header") or [])],
            "rows": [[str(c) for c in row] for row in (get("rows") or [])],
            "where": str(get("where") or "")}


def _flags(meta: dict[str, Any]) -> list[str]:
    flags = []
    if str(meta.get("no_text_layer")).lower() == "true":
        flags.append("no_text_layer")
    if str(meta.get("encrypted")).lower() == "true":
        flags.append("encrypted")
    return flags


def to_records(doc: Any, *, integration: str | None, received_at: str, sha256: str) -> list[dict[str, Any]]:
    """The parent first, then its children (depth first). Each is a V17 record without `id`/`seq`; a child's
    `parent_index` is the index of its parent in this list. `doc` is a `runner.documents.Document` (or anything
    with `kind, name, text, html, tables, meta, children`)."""
    out: list[dict[str, Any]] = []

    def walk(d: Any, parent_index: int | None, inherited: dict[str, Any], site: str | None) -> None:
        own = dict(getattr(d, "meta", None) or {})
        meta = {**inherited, **own}
        here = sender_domain(own) or site
        children = list(getattr(d, "children", None) or [])
        kind = str(getattr(d, "kind", "") or "")
        index = len(out)
        out.append({"parent_index": parent_index, "kind": kind, "integration": integration,
                    "site": here or "upload", "name": str(getattr(d, "name", "") or ""),
                    "text": str(getattr(d, "text", "") or ""), "html": getattr(d, "html", None) or "",
                    "tables": [_table_dict(t) for t in (getattr(d, "tables", None) or [])],
                    "meta": meta, "flags": _flags(own), "sha256": sha256,
                    "received_at": received_at, "received_date": received_at[:10],
                    "is_leaf": False, "_leaf_kind": kind in LEAF_KINDS})
        for c in children:
            walk(c, index, meta, here)

    walk(doc, None, {}, None)
    for i, rec in enumerate(out):
        # a child of kind pdf/sheet/html/text is a leaf; the parent is one only when it has no such child
        has_leaf_child = any(r["parent_index"] == i and r["_leaf_kind"] for r in out)
        rec["is_leaf"] = rec["_leaf_kind"] if rec["parent_index"] is not None else not has_leaf_child
    for rec in out:
        del rec["_leaf_kind"]
    return out


# ---- the inbox -----------------------------------------------------------------------------------------

class DocumentInbox:
    """SQLite-backed like `PageInbox`: each call opens its own connection, `seq` is AUTOINCREMENT, `clear()` keeps
    the counter so a document id is never reused within a workspace."""

    def __init__(self, path: str | Path | None = None):
        self._path = Path(path) if path else None
        with self._conn() as db:
            db.executescript(SCHEMA)

    @property
    def path(self) -> Path:
        return self._path or workspace_dir() / "documents.db"

    @property
    def originals_dir(self) -> Path:
        return self.path.parent / "documents"

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, isolation_level=None, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            deadline = time.monotonic() + 15
            while True:
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

    # -- writing ---------------------------------------------------------------------------------------
    def add_bytes(self, data: bytes, name: str, content_type: str = "", integration: str | None = None,
                  received_at: str | None = None, site: str | None = None,
                  meta: dict[str, Any] | None = None) -> dict[str, Any]:
        """Read `data` with `runner.documents.read_any`, store the document (and its children) and return the
        parent record plus `leaves: [ids]` and `duplicate`. The same bytes for one integration return the
        existing record with `duplicate: true`. A file that cannot be read is kept as one record flagged
        `unreadable` (its error in `meta.error` and `meta.problem`), so a person can see it."""
        name = Path(name or "upload").name or "upload"
        sha = hashlib.sha256(data).hexdigest()
        existing = self._by_sha(sha, integration)
        if existing is not None:
            return {**existing, "leaves": self._leaf_ids(existing["id"]), "duplicate": True}
        received_at = received_at or datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        try:
            from runner.documents import read_any  # the teammate's readers (Task 2)
        except ImportError as e:
            raise DocumentsUnavailable(f"document readers are not installed yet ({e})") from None
        try:
            records = to_records(read_any(data, name, content_type or ""), integration=integration,
                                 received_at=received_at, sha256=sha)
        except Exception as e:  # noqa: BLE001 - any unreadable file is kept and flagged, never lost
            problem = f"{type(e).__name__}: {e}"[:300]
            records = [{"parent_index": None, "kind": "file", "integration": integration, "site": "upload",
                        "name": name, "text": "", "html": "", "tables": [], "flags": ["unreadable"],
                        "meta": {"error": problem, "problem": problem}, "sha256": sha, "received_at": received_at,
                        "received_date": received_at[:10], "is_leaf": True}]
        for r in records:
            if site:
                r["site"] = site
            if meta:
                r["meta"] = {**r["meta"], **meta}
        self.originals_dir.mkdir(parents=True, exist_ok=True)
        (self.originals_dir / f"{sha[:16]}-{name}").write_bytes(data)
        seqs: list[int] = []
        with self._conn() as db:
            for r in records:
                parent = seqs[r["parent_index"]] if r["parent_index"] is not None else None
                row = {**{k: r[k] for k in COLUMNS if k not in ("seq", "parent_seq", *_JSON, "is_leaf")},
                       "parent_seq": parent, "is_leaf": int(r["is_leaf"]),
                       **{k: json.dumps(r[k]) for k in _JSON}}
                cols = list(row)
                cur = db.execute(f"INSERT INTO documents({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                                 [row[c] for c in cols])
                seqs.append(int(cur.lastrowid))
        parent_rec = self.get(doc_id(seqs[0]))
        assert parent_rec is not None
        return {**parent_rec, "leaves": self._leaf_ids(parent_rec["id"]), "duplicate": False}

    # -- reading ---------------------------------------------------------------------------------------
    def _by_sha(self, sha: str, integration: str | None) -> dict[str, Any] | None:
        with self._conn() as db:
            row = db.execute("SELECT * FROM documents WHERE sha256 = ? AND parent_seq IS NULL AND "
                             "integration IS ? ORDER BY seq LIMIT 1", (sha, integration)).fetchone()
        return _record(row) if row else None

    def _leaf_ids(self, parent_id: str) -> list[str]:
        seq = seq_of(parent_id)
        with self._conn() as db:
            rows = db.execute("SELECT seq FROM documents WHERE is_leaf = 1 AND (seq = ? OR parent_seq = ?) "
                              "ORDER BY seq", (seq, seq)).fetchall()
        return [doc_id(r["seq"]) for r in rows]

    def list(self, since: int = 0, limit: int = 100, integration: str | None = None,
             leaves_only: bool = True) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), MAX_LIMIT))
        cond = ["seq > ?"]
        args: list[Any] = [int(since)]
        if integration is not None:
            cond.append("integration = ?")
            args.append(integration)
        if leaves_only:
            cond.append("is_leaf = 1")
        with self._conn() as db:
            rows = db.execute(f"SELECT * FROM documents WHERE {' AND '.join(cond)} ORDER BY seq LIMIT ?",
                              (*args, limit)).fetchall()
        return [_record(r) for r in rows]

    def get(self, did: str) -> dict[str, Any] | None:
        seq = seq_of(did)
        if seq is None:
            return None
        with self._conn() as db:
            row = db.execute("SELECT * FROM documents WHERE seq = ?", (seq,)).fetchone()
        return _record(row) if row else None

    def summaries(self, limit: int | None = None, newest_first: bool = True, integration: str | None = None,
                  leaves_only: bool = True) -> list[dict[str, Any]]:
        """Records without `text`/`html`/`tables`/`meta` (cheap enough to poll)."""
        cond, args = ["1=1"], []
        if integration is not None:
            cond.append("integration = ?")
            args.append(integration)
        if leaves_only:
            cond.append("is_leaf = 1")
        sql = (f"SELECT {', '.join(SUMMARY_COLUMNS)} FROM documents WHERE {' AND '.join(cond)} "
               f"ORDER BY seq {'DESC' if newest_first else 'ASC'}")
        if limit is not None:
            sql += " LIMIT ?"
            args.append(max(1, int(limit)))
        with self._conn() as db:
            rows = db.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["flags"] = json.loads(d["flags"])
            d["is_leaf"] = bool(d["is_leaf"])
            d["id"] = doc_id(d["seq"])
            d["parent_id"] = doc_id(d.pop("parent_seq")) if d["parent_seq"] else None
            out.append(d)
        return out

    def count(self) -> int:
        with self._conn() as db:
            return int(db.execute("SELECT COUNT(*) FROM documents").fetchone()[0])

    def clear(self, integration: str | None = None) -> int:
        """Delete documents (and their learn links); returns the number removed. `seq` is never reused."""
        with self._conn() as db:
            if integration is None:
                n = int(db.execute("DELETE FROM documents").rowcount)
                db.execute("DELETE FROM learns")
                return n
            ids = [doc_id(r[0]) for r in db.execute("SELECT seq FROM documents WHERE integration = ?",
                                                    (integration,))]
            n = int(db.execute("DELETE FROM documents WHERE integration = ?", (integration,)).rowcount)
            db.execute("DELETE FROM learns WHERE integration = ?", (integration,))
            for i in range(0, len(ids), 500):
                part = ids[i:i + 500]
                db.execute(f"DELETE FROM learns WHERE page_id IN ({','.join('?' * len(part))})", part)
            return n

    # -- learn threads (same shape as PageInbox: `page_id` holds the document id) ------------------------
    def add_learn(self, page_id: str, thread_id: str, site: str, integration: str) -> None:
        with self._conn() as db:
            db.execute("INSERT OR REPLACE INTO learns VALUES (?,?,?,?,?)",
                       (page_id, thread_id, site, integration,
                        datetime.now(timezone.utc).isoformat(timespec="milliseconds")))

    def learns(self, integration: str | None = None) -> dict[str, dict[str, Any]]:
        sql, args = "SELECT * FROM learns", ()
        if integration:
            sql, args = sql + " WHERE integration = ?", (integration,)
        with self._conn() as db:
            return {r["page_id"]: dict(r) for r in db.execute(sql, args).fetchall()}


class DocumentsUnavailable(RuntimeError):
    """`runner.documents` (the readers) is not importable."""
