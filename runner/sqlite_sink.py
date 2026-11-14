"""SQLite sink (contracts V16). AI-free, like the rest of the runner. The Airtable sink's shape (V14), with a
local database file instead of a REST API:

    contract = fetch_contract(path, table)           # OpenAPI-shaped doc from PRAGMA table_xinfo, one op: POST /rows
    fields = convert_body(contract, body)            # typed per the column, or VALIDATION_FAILED
    rowid = send(path, table, fields, contract)      # one parameterised INSERT; errors -> Quarantine
    verify_read_back(path, table, rowid, fields, contract)   # VERIFY_MISMATCH on a difference

`SqliteSession` binds these for one run (the contract cache and the dry-run flag: a dry run reads the schema
and validates against it through a read-only connection and never INSERTs).

Safety rules, enforced here and not by callers:
  * `path` is resolved under $PATCHWORK_WORKSPACE and must stay inside it (symlinks included); a missing
    file is SINK_CONTRACT, never created (connections use the URI modes `rw` / `ro`, not `rwc`);
  * values are always bound parameters; column and table names are only ever spliced into SQL after they
    were read back from PRAGMA table_xinfo (a body key that is not a column is refused first) and are
    double-quote-escaped;
  * no DDL, no PRAGMA that writes, no journal-mode change on a database that is not ours.

Error classification: a missing file, table or column, or a read-only database -> SINK_CONTRACT; a violated
constraint (NOT NULL, UNIQUE, CHECK, FOREIGN KEY) -> VALIDATION_FAILED (the record is the problem, not the
destination); a locked database or anything else -> UNKNOWN.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from runner.airtable import _same, convert_value
from runner.contract import validate_body
from runner.lib import Quarantine

SINK_CALL = {"method": "POST", "path": "/rows"}  # the contract's one operation (manifest sink_call)
BUSY_TIMEOUT_S = 10.0


# ---- the file -----------------------------------------------------------------------------------

def resolve_path(path: str) -> Path:
    """`path` (relative to the workspace) as an existing file inside the workspace, else SINK_CONTRACT."""
    root = Path(os.environ.get("PATCHWORK_WORKSPACE", "workspace")).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise Quarantine("SINK_CONTRACT", f"sqlite path {path!r} leaves the workspace; refusing to open it")
    if not target.is_file():
        raise Quarantine("SINK_CONTRACT", f"sqlite database {path!r} not found in the workspace")
    return target


def _connect(path: str, *, readonly: bool) -> sqlite3.Connection:
    target = resolve_path(path)
    uri = f"file:{quote(str(target))}?mode={'ro' if readonly else 'rw'}"
    try:
        return sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_S, isolation_level=None)
    except sqlite3.Error as e:
        raise Quarantine("SINK_CONTRACT", f"sqlite database {path!r} could not be opened: {e}") from None


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# ---- contract -----------------------------------------------------------------------------------

def column_schema(declared: str) -> dict | None:
    """A column's declared type -> its JSON-schema property, by SQLite's own affinity rules (plus the
    BOOL / DATE / DATETIME names people use). None for BLOB: never write a value we cannot type."""
    d = (declared or "").upper()
    if "BOOL" in d:
        prop: dict[str, Any] = {"type": "boolean"}
    elif "DATETIME" in d or "TIMESTAMP" in d:
        prop = {"type": "string", "format": "date-time"}
    elif "DATE" in d:
        prop = {"type": "string", "format": "date"}
    elif "INT" in d:
        prop = {"type": "integer"}
    elif "CHAR" in d or "CLOB" in d or "TEXT" in d or d == "":
        prop = {"type": "string"}
    elif "BLOB" in d:
        return None
    else:  # REAL / FLOA / DOUB, and NUMERIC affinity (NUMERIC, DECIMAL, ...)
        prop = {"type": "number"}
    prop["x-sqlite-type"] = declared or ""
    return prop


def contract_from_conn(conn: sqlite3.Connection, table: str, path: str) -> dict:
    """The V16 contract document for `table`. Only column names, types and required flags go in, so the hash
    changes on a rename, retype or a new required column and nowhere else."""
    rows = conn.execute(f"PRAGMA table_xinfo({_ident(table)})").fetchall()
    if not rows:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                                             "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        raise Quarantine("SINK_CONTRACT", f"table {table!r} not found in {path!r} (tables: {', '.join(map(repr, tables))})")
    pks = [r for r in rows if r[5]]
    props: dict[str, dict] = {}
    required: list[str] = []
    for _cid, name, declared, notnull, default, pk, hidden in rows:
        if hidden:  # generated and virtual-table columns are not writable
            continue
        prop = column_schema(declared)
        if prop is None:
            continue
        props[name] = prop
        rowid_alias = len(pks) == 1 and bool(pk) and (declared or "").upper() == "INTEGER"
        if notnull and default is None and not rowid_alias:
            required.append(name)
    schema: dict[str, Any] = {"type": "object", "additionalProperties": False, "properties": props}
    if required:
        schema["required"] = required
    return {"openapi": "3.0.3",
            "info": {"title": table, "x-patchwork-kind": "sqlite", "x-sqlite-path": path},
            "paths": {SINK_CALL["path"]: {"post": {"requestBody": {"content": {"application/json": {"schema": schema}}}}}}}


def fetch_contract(path: str, table: str) -> dict:
    """The table's V16 contract, read through a read-only connection. Raises Quarantine(SINK_CONTRACT)."""
    with closing(_connect(path, readonly=True)) as conn:
        try:
            return contract_from_conn(conn, table, path)
        except sqlite3.Error as e:
            q = _classify(e, "schema read")
            raise (q if q.code != "UNKNOWN" else Quarantine("UNKNOWN", f"sqlite table {table!r} could not be read: {e}")) from None


def properties(contract: dict) -> dict[str, dict]:
    return (contract["paths"][SINK_CALL["path"]]["post"]["requestBody"]["content"]["application/json"]
            ["schema"].get("properties", {}))


def field_types(contract: dict | None, names: list[str]) -> dict[str, dict]:
    """The typing facts of `names` (type, format, x-sqlite-type) for a compiled package."""
    if not contract:
        return {}
    props = properties(contract)
    keep = ("type", "format", "x-sqlite-type")
    return {n: {k: props[n][k] for k in keep if k in props[n]} for n in names if n in props}


# ---- typed values -------------------------------------------------------------------------------

def _convert(prop: dict, value: Any) -> Any:
    if prop.get("type") == "integer":
        n = convert_value({"type": "number"}, value)
        if isinstance(n, float):
            raise ValueError(f"{value!r} is not a whole number")
        return n
    return convert_value(prop, value)


def convert_values(props: dict[str, dict], body: dict, *, drop_empty: bool = True) -> dict:
    """The body typed per `props` (a contract's properties, or the compile-time FIELD_TYPES). With
    `drop_empty`, None/"" are left out (the column keeps its default or NULL); otherwise they are kept as is.
    Unknown names pass through: the contract check (validate_body) reports them.
    Raises Quarantine("VALIDATION_FAILED") naming every value that does not fit its column."""
    out, errors = {}, []
    for k, v in body.items():
        if v is None or v == "":
            if not drop_empty:
                out[k] = v
            continue
        if k not in props:
            out[k] = v
            continue
        try:
            out[k] = _convert(props[k], v)
        except (ValueError, TypeError) as e:
            errors.append(f"{k} ({props[k].get('x-sqlite-type') or 'untyped'}): {e}")
    if errors:
        raise Quarantine("VALIDATION_FAILED", "value does not fit its SQLite column: " + "; ".join(errors))
    return out


def convert_body(contract: dict, body: dict, *, drop_empty: bool = True) -> dict:
    return convert_values(properties(contract), body, drop_empty=drop_empty)


# ---- write and read back ------------------------------------------------------------------------

def _writable(fields: dict, contract: dict) -> list[str]:
    """The column names to splice into SQL: every key must be a column of the contract, or the write is refused
    (this is what keeps a spec's field name from ever reaching the SQL text)."""
    props = properties(contract)
    unknown = [k for k in fields if k not in props]
    if unknown:
        raise Quarantine("SINK_CONTRACT", f"no such column in the table: {', '.join(map(repr, unknown))}")
    return list(fields)


def _classify(e: sqlite3.Error, what: str) -> Quarantine:
    msg = str(e)
    if isinstance(e, sqlite3.IntegrityError):
        return Quarantine("VALIDATION_FAILED", f"the table refused the row on {what}: {msg}")
    low = msg.lower()
    if isinstance(e, sqlite3.OperationalError) and ("locked" in low or "busy" in low):
        return Quarantine("UNKNOWN", f"database is locked on {what} (waited {BUSY_TIMEOUT_S:.0f} s): {msg}")
    if isinstance(e, sqlite3.OperationalError) and ("no such" in low or "readonly" in low or "no column" in low
                                                    or "has no column" in low):
        return Quarantine("SINK_CONTRACT", f"sqlite {what} failed: {msg}")
    return Quarantine("UNKNOWN", f"sqlite {what} failed: {msg}")


def send(path: str, table: str, fields: dict, contract: dict) -> int:
    """INSERT one row (bound parameters) -> its rowid. An empty `fields` inserts DEFAULT VALUES."""
    cols = _writable(fields, contract)
    if cols:
        sql = (f"INSERT INTO {_ident(table)} ({', '.join(_ident(c) for c in cols)}) "
               f"VALUES ({', '.join('?' for _ in cols)})")
    else:
        sql = f"INSERT INTO {_ident(table)} DEFAULT VALUES"
    with closing(_connect(path, readonly=False)) as conn:
        try:
            cur = conn.execute(sql, [fields[c] for c in cols])
            return cur.lastrowid
        except sqlite3.Error as e:
            raise _classify(e, "insert") from None


def compare_fields(sent: dict, got: dict, contract: dict | None = None) -> list[str]:
    props = properties(contract) if contract else {}
    diffs = []
    for k, v in sent.items():
        prop = props.get(k)
        if (prop or {}).get("type") == "boolean":
            if bool(v) != bool(got.get(k)):
                diffs.append(f"{k}: sent {v!r}, stored {got.get(k)!r}")
        elif k not in got or got[k] is None:
            diffs.append(f"{k}: sent {v!r}, stored nothing")
        elif not _same(prop, v, got[k]):
            diffs.append(f"{k}: sent {v!r}, stored {got[k]!r}")
    return diffs


def verify_read_back(path: str, table: str, rowid: int, sent: dict, contract: dict) -> None:
    """SELECT the row back by rowid and compare every sent column; a difference is VERIFY_MISMATCH."""
    cols = _writable(sent, contract)
    if not cols:
        return
    with closing(_connect(path, readonly=True)) as conn:
        try:
            row = conn.execute(f"SELECT {', '.join(_ident(c) for c in cols)} FROM {_ident(table)} WHERE rowid = ?",
                               (rowid,)).fetchone()
        except sqlite3.Error as e:
            raise Quarantine("VERIFY_MISMATCH", f"row {rowid} could not be read back: {e}") from None
    if row is None:
        raise Quarantine("VERIFY_MISMATCH", f"row {rowid} is not in {table!r} after the insert")
    diffs = compare_fields(sent, dict(zip(cols, row)), contract)
    if diffs:
        raise Quarantine("VERIFY_MISMATCH", f"row {rowid} read back differently: " + "; ".join(diffs))


@dataclass
class SqliteResult:
    """Response-like (runner/pipeline.py reads status_code, text and detail)."""

    status_code: int
    text: str = ""
    detail: str | None = None
    rowid: int | None = None


class SqliteSession:
    """One run's SQLite binding: contract cache and the dry-run guarantee."""

    def __init__(self, path: str, table: str, *, read_back: bool = True, dry_run: bool = False):
        self.path, self.table, self.read_back, self.dry_run = path, table, read_back, dry_run
        self._contract: dict | None = None
        self.inserts = 0  # for tests: statements that wrote

    def contract(self, refresh: bool = False) -> dict:
        if self._contract is None or refresh:
            self._contract = fetch_contract(self.path, self.table)
        return self._contract

    def submit(self, body: dict) -> SqliteResult:
        contract = self.contract(refresh=True)  # the live schema, every record: a rename never slips through
        fields = convert_body(contract, body)
        errors = validate_body(contract, SINK_CALL["method"], SINK_CALL["path"], fields)
        if errors:
            raise Quarantine("SINK_CONTRACT", "; ".join(errors))
        if self.dry_run:  # V16: a dry run reads the schema and validates the body; never INSERTs
            return SqliteResult(200, "", "dry run: validated against the table, not inserted")
        rowid = send(self.path, self.table, fields, contract)
        self.inserts += 1
        if self.read_back:
            verify_read_back(self.path, self.table, rowid, fields, contract)
        return SqliteResult(200, str(rowid), None, rowid)

    def close(self) -> None:
        return None
