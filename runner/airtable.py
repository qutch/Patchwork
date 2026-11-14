"""Airtable sink (contracts V14). AI-free, like the rest of the runner.

    contract = fetch_contract(base_id, table, headers)        # OpenAPI-shaped doc, one op: POST /records
    fields = convert_body(contract, body)                     # typed per the contract, or VALIDATION_FAILED
    record_id = send(base_id, table, fields, headers)         # paced >= 0.22 s per base; errors -> Quarantine
    verify_read_back(base_id, table, record_id, fields, headers, contract)   # VERIFY_MISMATCH on a difference

`AirtableSession` binds these for one run (one httpx client, the run's credential headers, the contract
cache and the dry-run flag: a dry run converts and validates against the live contract and never POSTs).
The API base is https://api.airtable.com/v0, overridden by PATCHWORK_AIRTABLE_API_URL (the fake in tests
and the offline smoke). Error classification: 422 -> SINK_CONTRACT (Airtable's error type and message),
401/403 -> AUTH (names the credential, never its value), 429 -> wait 30 s and retry once, then UNKNOWN,
any other >= 400 -> UNKNOWN.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable
from urllib.parse import quote

import httpx

from runner.contract import validate_body
from runner.lib import Quarantine

API_URL = "https://api.airtable.com/v0"
SINK_CALL = {"method": "POST", "path": "/records"}  # the contract's one operation (manifest sink_call)
MIN_INTERVAL_S = 0.22  # Airtable allows 5 requests/s per base
RATE_LIMIT_WAIT_S = 30.0

_STRING = {"singleLineText", "multilineText", "richText", "phoneNumber"}
_NUMBER = {"number", "currency", "percent", "rating", "duration"}
# Computed or not writable as plain values: omitted from the contract. Types this module does not know
# (collaborators, barcodes, AI text, ...) are omitted too: never write a value we cannot type.
COMPUTED = {"formula", "rollup", "lookup", "multipleLookupValues", "count", "autoNumber", "createdTime",
            "lastModifiedTime", "createdBy", "lastModifiedBy", "button", "multipleRecordLinks",
            "multipleAttachments", "attachments"}


def api_url() -> str:
    return (os.environ.get("PATCHWORK_AIRTABLE_API_URL") or API_URL).rstrip("/")


# ---- contract --------------------------------------------------------------------------------

def field_schema(field: dict) -> dict | None:
    """One Airtable field (meta API shape) -> its JSON-schema property, or None if it is not writable."""
    t = field.get("type")
    opts = field.get("options") or {}
    if t in _STRING:
        prop: dict[str, Any] = {"type": "string"}
    elif t == "email":
        prop = {"type": "string", "format": "email"}
    elif t == "url":
        prop = {"type": "string", "format": "uri"}
    elif t in _NUMBER:
        prop = {"type": "number"}
    elif t == "checkbox":
        prop = {"type": "boolean"}
    elif t == "date":
        prop = {"type": "string", "format": "date"}
    elif t == "dateTime":
        prop = {"type": "string", "format": "date-time"}
    elif t == "singleSelect":
        prop = {"type": "string", "enum": [c["name"] for c in opts.get("choices") or []]}
    elif t == "multipleSelects":
        prop = {"type": "array", "items": {"type": "string", "enum": [c["name"] for c in opts.get("choices") or []]}}
    else:
        return None
    prop["x-airtable-type"] = t
    return prop


def contract_from_tables(tables: list[dict], table: str, base_id: str) -> dict:
    """The V14 contract document for `table` (an id or an exact name) from a meta-API `tables` list.
    Only names, types and options go in (never field ids or descriptions), so the hash changes on a
    rename or retype and nowhere else."""
    found = next((t for t in tables if t.get("id") == table), None) or \
        next((t for t in tables if t.get("name") == table), None)
    if found is None:
        names = ", ".join(repr(t.get("name")) for t in tables)
        raise Quarantine("SINK_CONTRACT", f"table {table!r} not found in Airtable base {base_id} (tables: {names})")
    props: dict[str, dict] = {}
    for f in found.get("fields") or []:
        prop = field_schema(f)
        if prop is not None and f.get("name"):
            props[f["name"]] = prop
    schema = {"type": "object", "additionalProperties": False, "properties": props}
    return {"openapi": "3.0.3",
            "info": {"title": found.get("name") or table, "x-patchwork-kind": "airtable", "x-airtable-base": base_id},
            "paths": {SINK_CALL["path"]: {"post": {"requestBody": {"content": {"application/json": {"schema": schema}}}}}}}


def properties(contract: dict) -> dict[str, dict]:
    return (contract["paths"][SINK_CALL["path"]]["post"]["requestBody"]["content"]["application/json"]
            ["schema"].get("properties", {}))


def _raise_for(resp: Any, what: str, credential: str | None) -> None:
    status = resp.status_code
    if status < 300:
        return
    if status in (401, 403):
        raise Quarantine("AUTH", f"Airtable rejected the credential {credential or 'the Airtable token'} (HTTP {status}) on {what}")
    if status in (400, 422):
        etype, msg = _error_of(resp)
        raise Quarantine("SINK_CONTRACT", f"Airtable {status} {etype}: {msg}" if etype else f"Airtable {status}: {msg}")
    if status == 404:
        etype, msg = _error_of(resp)
        raise Quarantine("SINK_CONTRACT", f"Airtable 404 {etype or 'NOT_FOUND'} on {what}: {msg}")
    etype, msg = _error_of(resp)
    raise Quarantine("UNKNOWN", f"Airtable {status} on {what}: {etype} {msg}".strip())


def _error_of(resp: Any) -> tuple[str, str]:
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return "", (getattr(resp, "text", "") or "")[:300]
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("type") or ""), str(err.get("message") or "")[:300]
    if isinstance(err, str):
        return err, str(data.get("message") or "")[:300]
    return "", str(data)[:300]


class Pacer:
    """At most one request per MIN_INTERVAL_S per base (process-wide). `clock`/`sleep` are injectable."""

    def __init__(self, interval: float = MIN_INTERVAL_S, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.interval, self.clock, self.sleep = interval, clock, sleep
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, base_id: str) -> None:
        with self._lock:
            last = self._last.get(base_id)
            now = self.clock()
            if last is not None and now - last < self.interval:
                self.sleep(self.interval - (now - last))
                now = self.clock()
            self._last[base_id] = now


PACER = Pacer()


def _request(client: httpx.Client, method: str, url: str, *, base_id: str, headers: dict, pacer: Pacer,
             what: str, credential: str | None, json: Any = None) -> Any:
    """One paced request; 429 waits RATE_LIMIT_WAIT_S and retries once, then UNKNOWN."""
    for attempt in (1, 2):
        pacer.wait(base_id)
        resp = client.request(method, url, headers=headers, json=json)
        if resp.status_code != 429:
            _raise_for(resp, what, credential)
            return resp
        if attempt == 1:
            pacer.sleep(RATE_LIMIT_WAIT_S)
    raise Quarantine("UNKNOWN", f"Airtable rate limit (HTTP 429) on {what}, still limited after waiting "
                                f"{RATE_LIMIT_WAIT_S:.0f} s")


def _table_path(table: str) -> str:
    return quote(table, safe="")


def fetch_contract(base_id: str, table: str, headers: dict, *, client: httpx.Client | None = None,
                   pacer: Pacer | None = None, credential: str | None = None) -> dict:
    """GET /meta/bases/{base_id}/tables -> the V14 contract of `table`. Raises Quarantine (AUTH,
    SINK_CONTRACT for an unknown table, UNKNOWN)."""
    own = client is None
    client = client or httpx.Client(timeout=15)
    try:
        resp = _request(client, "GET", f"{api_url()}/meta/bases/{base_id}/tables", base_id=base_id, headers=headers,
                        pacer=pacer or PACER, what="the table schema", credential=credential)
        return contract_from_tables(resp.json().get("tables") or [], table, base_id)
    finally:
        if own:
            client.close()


# ---- typed values ------------------------------------------------------------------------------

_TRUE = {"true", "yes", "y", "1", "checked", "on", "x"}
_FALSE = {"false", "no", "n", "0", "unchecked", "off", ""}
_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T ].*)?$")


def _number(v: Any) -> int | float:
    if isinstance(v, bool):
        raise ValueError("a checkbox value is not a number")
    if isinstance(v, (int, float)):
        return v
    s = str(v).strip().replace(",", "")
    f = float(s)
    return int(f) if f.is_integer() and re.fullmatch(r"[-+]?\d+(\.0*)?", s) else f


def convert_value(prop: dict, value: Any, *, typecast: bool = False) -> Any:
    """`value` as Airtable wants it for this property (number -> JSON number, checkbox -> bool, date ->
    YYYY-MM-DD, select -> one of its choices). ValueError if it does not fit."""
    t = prop.get("type")
    if t == "number":
        return _number(value)
    if t == "boolean":
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
        raise ValueError(f"{value!r} is not a checkbox value (true/false)")
    if t == "array":
        items = value if isinstance(value, list) else [x.strip() for x in str(value).split(",") if x.strip()]
        enum = (prop.get("items") or {}).get("enum")
        out = [str(x) for x in items]
        bad = [x for x in out if enum is not None and x not in enum]
        if bad and not typecast:
            raise ValueError(f"{bad} not among the choices {enum}")
        return out
    # strings
    if isinstance(value, (dict, list)):
        raise ValueError(f"{type(value).__name__} is not a text value")
    fmt = prop.get("format")
    if fmt == "date":
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        m = _DATE.match(str(value).strip())
        if not m:
            raise ValueError(f"{value!r} is not a date (YYYY-MM-DD)")
        date.fromisoformat(m.group(1))
        return m.group(1)
    if fmt == "date-time":
        if isinstance(value, datetime):
            return value.isoformat()
        s = str(value).strip()
        datetime.fromisoformat(s.replace("Z", "+00:00"))
        return s
    s = value if isinstance(value, str) else str(value)
    if "enum" in prop and s not in prop["enum"] and not typecast:
        raise ValueError(f"{s!r} is not one of the choices {prop['enum']}")
    return s


def convert_values(props: dict[str, dict], body: dict, *, typecast: bool = False, drop_empty: bool = True) -> dict:
    """The body typed per `props` (a contract's properties, or the compile-time FIELD_TYPES). With
    `drop_empty`, None/"" values are left out (Airtable stores them as empty); otherwise they are kept as is.
    Unknown field names are passed through unchanged: the contract check (validate_body) reports them.
    Raises Quarantine("VALIDATION_FAILED") naming every value that does not fit its field."""
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
            out[k] = convert_value(props[k], v, typecast=typecast)
        except (ValueError, TypeError) as e:
            errors.append(f"{k} ({props[k].get('x-airtable-type')}): {e}")
    if errors:
        raise Quarantine("VALIDATION_FAILED", "value does not fit its Airtable field: " + "; ".join(errors))
    return out


def convert_body(contract: dict, body: dict, *, typecast: bool = False, drop_empty: bool = True) -> dict:
    """`convert_values` against a V14 contract document."""
    return convert_values(properties(contract), body, typecast=typecast, drop_empty=drop_empty)


def field_types(contract: dict | None, names: list[str]) -> dict[str, dict]:
    """The typing facts of `names` (type, format, enum, items, x-airtable-type) for a compiled package."""
    if not contract:
        return {}
    props = properties(contract)
    keep = ("type", "format", "enum", "items", "x-airtable-type")
    return {n: {k: props[n][k] for k in keep if k in props[n]} for n in names if n in props}


# ---- write and read back -----------------------------------------------------------------------

def send(base_id: str, table: str, fields: dict, headers: dict, *, typecast: bool = False,
         client: httpx.Client | None = None, pacer: Pacer | None = None, credential: str | None = None) -> str:
    """POST /{base_id}/{table} {"records": [{"fields": fields}], "typecast": typecast} -> the new record id."""
    own = client is None
    client = client or httpx.Client(timeout=15)
    try:
        resp = _request(client, "POST", f"{api_url()}/{base_id}/{_table_path(table)}", base_id=base_id,
                        headers=headers, pacer=pacer or PACER, what="create record", credential=credential,
                        json={"records": [{"fields": fields}], "typecast": bool(typecast)})
        try:
            return resp.json()["records"][0]["id"]
        except (KeyError, IndexError, TypeError, ValueError):
            raise Quarantine("UNKNOWN", f"Airtable answered {resp.status_code} without a record id") from None
    finally:
        if own:
            client.close()


def _norm(s: Any) -> str:
    return " ".join(str(s).split())


def _same(prop: dict | None, sent: Any, got: Any) -> bool:
    t = (prop or {}).get("type")
    if t == "boolean":
        return bool(sent) == bool(got)
    if got is None:
        return False
    if t == "number":
        try:
            return abs(float(sent) - float(got)) <= 1e-9 * max(1.0, abs(float(sent)))
        except (TypeError, ValueError):
            return False
    if t == "array":
        return isinstance(got, list) and [_norm(x) for x in sent] == [_norm(x) for x in got]
    fmt = (prop or {}).get("format")
    if fmt == "date":
        try:
            return date.fromisoformat(str(sent)[:10]) == date.fromisoformat(str(got)[:10])
        except ValueError:
            return False
    if fmt == "date-time":
        try:
            a = datetime.fromisoformat(str(sent).replace("Z", "+00:00"))
            b = datetime.fromisoformat(str(got).replace("Z", "+00:00"))
            return a == b if (a.tzinfo is None) == (b.tzinfo is None) else a.replace(tzinfo=None) == b.replace(tzinfo=None)
        except ValueError:
            return False
    return _norm(sent) == _norm(got)


def compare_fields(sent: dict, got: dict, contract: dict | None = None) -> list[str]:
    """Differences between what was sent and what the record holds (empty = the same)."""
    props = properties(contract) if contract else {}
    diffs = []
    for k, v in sent.items():
        prop = props.get(k)
        if (prop or {}).get("type") == "boolean":
            if bool(v) != bool(got.get(k, False)):
                diffs.append(f"{k}: sent {v!r}, stored {got.get(k)!r}")
            continue
        if k not in got:
            diffs.append(f"{k}: sent {v!r}, stored nothing")
        elif not _same(prop, v, got[k]):
            diffs.append(f"{k}: sent {v!r}, stored {got[k]!r}")
    return diffs


def verify_read_back(base_id: str, table: str, record_id: str, sent: dict, headers: dict, contract: dict | None = None,
                     *, client: httpx.Client | None = None, pacer: Pacer | None = None,
                     credential: str | None = None) -> None:
    """GET the record back and compare every sent field; a difference is VERIFY_MISMATCH."""
    own = client is None
    client = client or httpx.Client(timeout=15)
    try:
        try:
            resp = _request(client, "GET", f"{api_url()}/{base_id}/{_table_path(table)}/{record_id}", base_id=base_id,
                            headers=headers, pacer=pacer or PACER, what="read back", credential=credential)
        except Quarantine as q:
            if q.code == "SINK_CONTRACT":
                raise Quarantine("VERIFY_MISMATCH", f"record {record_id} could not be read back: {q.detail}") from None
            raise
        got = (resp.json() or {}).get("fields") or {}
        diffs = compare_fields(sent, got, contract)
        if diffs:
            raise Quarantine("VERIFY_MISMATCH", f"record {record_id} read back differently: " + "; ".join(diffs))
    finally:
        if own:
            client.close()


@dataclass
class AirtableResult:
    """Response-like (runner/pipeline.py reads status_code, text and detail)."""

    status_code: int
    text: str = ""
    detail: str | None = None
    record_id: str | None = None


class AirtableSession:
    """One run's Airtable binding: client, credential headers, contract cache, dry-run guarantee."""

    def __init__(self, base_id: str, table: str, headers: dict, *, credential: str | None = None,
                 typecast: bool = False, read_back: bool = True, dry_run: bool = False,
                 pacer: Pacer | None = None, client: httpx.Client | None = None):
        self.base_id, self.table, self.headers = base_id, table, headers
        self.credential, self.typecast, self.read_back, self.dry_run = credential, typecast, read_back, dry_run
        self.pacer = pacer or PACER
        self.client = client or httpx.Client(timeout=15)
        self._contract: dict | None = None
        self.posts = 0  # for tests: requests that wrote

    def contract(self, refresh: bool = False) -> dict:
        if self._contract is None or refresh:
            self._contract = fetch_contract(self.base_id, self.table, self.headers, client=self.client,
                                            pacer=self.pacer, credential=self.credential)
        return self._contract

    def submit(self, body: dict) -> AirtableResult:
        contract = self.contract()
        fields = convert_body(contract, body, typecast=self.typecast)
        errors = validate_body(contract, SINK_CALL["method"], SINK_CALL["path"], fields)
        if errors:
            raise Quarantine("SINK_CONTRACT", "; ".join(errors))
        if self.dry_run:  # V14: a dry run fetches the contract and validates the body; never POSTs
            return AirtableResult(200, "", "dry run: validated against the table, not sent")
        record_id = send(self.base_id, self.table, fields, self.headers, typecast=self.typecast, client=self.client,
                         pacer=self.pacer, credential=self.credential)
        self.posts += 1
        if self.read_back:
            verify_read_back(self.base_id, self.table, record_id, fields, self.headers, contract, client=self.client,
                             pacer=self.pacer, credential=self.credential)
        return AirtableResult(200, record_id, None, record_id)

    def close(self) -> None:
        self.client.close()
