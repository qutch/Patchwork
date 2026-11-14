"""SQLite metadata: integrations + active pointer, versions, runs, records, processed keys,
cursors and incidents. Artifacts (specs, generated packages) live on the filesystem."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS integrations (
  name TEXT PRIMARY KEY,
  active_version INTEGER
);
CREATE TABLE IF NOT EXISTS versions (
  name TEXT NOT NULL,
  version INTEGER NOT NULL,
  spec_hash TEXT NOT NULL,
  openapi_hash TEXT,
  tests_passed INTEGER NOT NULL DEFAULT 0,
  tests_summary TEXT,
  created_at TEXT NOT NULL,
  deployed_at TEXT,
  PRIMARY KEY (name, version)
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  integration TEXT NOT NULL,
  version INTEGER NOT NULL,
  dry_run INTEGER NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  duration_s REAL,
  counts TEXT,
  llm_calls INTEGER NOT NULL DEFAULT 0,
  incident_id INTEGER,
  message TEXT
);
CREATE TABLE IF NOT EXISTS records (
  run_id INTEGER NOT NULL,
  record_id TEXT NOT NULL,
  status TEXT NOT NULL,
  reason TEXT,
  detail TEXT,
  parser TEXT,
  route_value TEXT,
  dedupe_key TEXT,
  sink_body TEXT,
  raw TEXT NOT NULL,
  PRIMARY KEY (run_id, record_id)
);
CREATE TABLE IF NOT EXISTS processed (
  integration TEXT NOT NULL,
  scope TEXT NOT NULL,
  key TEXT NOT NULL,
  run_id INTEGER,
  processed_at TEXT NOT NULL,
  PRIMARY KEY (integration, scope, key)
);
CREATE TABLE IF NOT EXISTS cursors (
  integration TEXT NOT NULL,
  scope TEXT NOT NULL,
  cursor TEXT,
  PRIMARY KEY (integration, scope)
);
CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  integration TEXT NOT NULL,
  version INTEGER NOT NULL,
  run_id INTEGER,
  type TEXT NOT NULL,
  reason_codes TEXT NOT NULL,
  summary TEXT NOT NULL,
  detail TEXT,
  samples TEXT,
  old_openapi TEXT,
  new_openapi TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def workspace_dir() -> Path:
    return Path(os.environ.get("PATCHWORK_WORKSPACE", "workspace")).resolve()


class Store:
    def __init__(self, path: str | Path | None = None):
        path = Path(path) if path else workspace_dir() / "patchwork.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def _one(self, sql: str, *args: Any) -> dict | None:
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, *args: Any) -> list[dict]:
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    # -- integrations & versions ------------------------------------------------------
    def add_version(self, name: str, version: int, spec_hash: str, openapi_hash: str | None) -> None:
        self.db.execute("INSERT OR IGNORE INTO integrations(name, active_version) VALUES (?, NULL)", (name,))
        self.db.execute(
            "INSERT INTO versions(name, version, spec_hash, openapi_hash, created_at) VALUES (?,?,?,?,?)",
            (name, version, spec_hash, openapi_hash, now()),
        )

    def set_tests(self, name: str, version: int, passed: bool, summary: str) -> None:
        self.db.execute("UPDATE versions SET tests_passed=?, tests_summary=? WHERE name=? AND version=?",
                        (int(passed), summary, name, version))

    def version(self, name: str, version: int) -> dict | None:
        return self._one("SELECT * FROM versions WHERE name=? AND version=?", name, version)

    def versions(self, name: str) -> list[dict]:
        return self._all("SELECT * FROM versions WHERE name=? ORDER BY version", name)

    def latest_version(self, name: str) -> int:
        row = self._one("SELECT MAX(version) AS v FROM versions WHERE name=?", name)
        return (row or {}).get("v") or 0

    def active_version(self, name: str) -> int | None:
        row = self._one("SELECT active_version FROM integrations WHERE name=?", name)
        return row["active_version"] if row else None

    def set_active(self, name: str, version: int) -> None:
        self.db.execute("UPDATE integrations SET active_version=? WHERE name=?", (version, name))
        self.db.execute("UPDATE versions SET deployed_at=? WHERE name=? AND version=?", (now(), name, version))

    def integrations(self) -> list[dict]:
        return self._all("SELECT * FROM integrations ORDER BY name")

    # -- runs & records ------------------------------------------------------------------
    def start_run(self, name: str, version: int, dry_run: bool) -> int:
        cur = self.db.execute(
            "INSERT INTO runs(integration, version, dry_run, status, started_at) VALUES (?,?,?,?,?)",
            (name, version, int(dry_run), "running", now()),
        )
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, counts: dict, duration_s: float,
                   incident_id: int | None = None, message: str | None = None) -> None:
        self.db.execute(
            "UPDATE runs SET status=?, finished_at=?, duration_s=?, counts=?, incident_id=?, message=? WHERE id=?",
            (status, now(), round(duration_s, 3), json.dumps(counts), incident_id, message, run_id),
        )

    def run(self, run_id: int) -> dict | None:
        r = self._one("SELECT * FROM runs WHERE id=?", run_id)
        if r and r["counts"]:
            r["counts"] = json.loads(r["counts"])
        return r

    def runs(self, name: str | None = None, limit: int = 20) -> list[dict]:
        if name:
            rows = self._all("SELECT * FROM runs WHERE integration=? ORDER BY id DESC LIMIT ?", name, limit)
        else:
            rows = self._all("SELECT * FROM runs ORDER BY id DESC LIMIT ?", limit)
        for r in rows:
            r["counts"] = json.loads(r["counts"]) if r["counts"] else {}
        return rows

    def add_record(self, run_id: int, outcome: Any, raw: dict) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO records VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, outcome.record_id, outcome.status, outcome.reason, outcome.detail, outcome.parser,
             outcome.route_value, outcome.key,
             json.dumps(outcome.body) if outcome.body is not None else None, json.dumps(raw)),
        )

    def records(self, run_id: int) -> list[dict]:
        rows = self._all("SELECT * FROM records WHERE run_id=? ORDER BY record_id", run_id)
        for r in rows:
            r["sink_body"] = json.loads(r["sink_body"]) if r["sink_body"] else None
            r["raw"] = json.loads(r["raw"])
        return rows

    # -- idempotency & cursors ----------------------------------------------------------
    def is_processed(self, name: str, scope: str, key: str) -> bool:
        return self._one("SELECT 1 FROM processed WHERE integration=? AND scope=? AND key=?", name, scope, key) is not None

    def mark_processed(self, name: str, scope: str, key: str, run_id: int | None) -> None:
        self.db.execute("INSERT OR IGNORE INTO processed VALUES (?,?,?,?,?)", (name, scope, key, run_id, now()))

    def get_cursor(self, name: str, scope: str) -> str | None:
        row = self._one("SELECT cursor FROM cursors WHERE integration=? AND scope=?", name, scope)
        return row["cursor"] if row else None

    def set_cursor(self, name: str, scope: str, cursor: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO cursors VALUES (?,?,?)", (name, scope, cursor))

    # -- incidents -------------------------------------------------------------------------
    def open_incident(self, *, name: str, version: int, run_id: int | None, type: str,
                      reason_codes: list[str], summary: str, detail: Any = None, samples: Any = None,
                      old_openapi: Any = None, new_openapi: Any = None) -> int:
        cur = self.db.execute(
            "INSERT INTO incidents(integration, version, run_id, type, reason_codes, summary, detail, samples,"
            " old_openapi, new_openapi, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (name, version, run_id, type, json.dumps(reason_codes), summary,
             json.dumps(detail) if detail is not None else None,
             json.dumps(samples) if samples is not None else None,
             json.dumps(old_openapi) if old_openapi is not None else None,
             json.dumps(new_openapi) if new_openapi is not None else None, now()),
        )
        return int(cur.lastrowid)

    def incident(self, incident_id: int) -> dict | None:
        r = self._one("SELECT * FROM incidents WHERE id=?", incident_id)
        if r:
            for k in ("reason_codes", "detail", "samples", "old_openapi", "new_openapi"):
                r[k] = json.loads(r[k]) if r[k] else None
        return r

    def incidents(self, name: str | None = None, status: str | None = "open") -> list[dict]:
        sql, args = "SELECT id, integration, version, run_id, type, reason_codes, summary, status, created_at FROM incidents WHERE 1=1", []
        if name:
            sql += " AND integration=?"
            args.append(name)
        if status:
            sql += " AND status=?"
            args.append(status)
        rows = self._all(sql + " ORDER BY id DESC", *args)
        for r in rows:
            r["reason_codes"] = json.loads(r["reason_codes"])
        return rows
