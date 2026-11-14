"""Fake Airtable (contracts V14): an offline stand-in for the Airtable REST API, for tests and the offline smoke.

Point the runner at it with PATCHWORK_AIRTABLE_API_URL=<url>/v0. It enforces what the real API enforces and
the sink relies on: a bearer token, field names that exist (422 UNKNOWN_FIELD_NAME), values that fit the
field's type (422 INVALID_VALUE_FOR_COLUMN), and no writes to computed fields.

    GET  /v0/meta/bases/{base}/tables         schema (the meta API's shape: tables -> fields {id, name, type, options})
    POST /v0/{base}/{table}                   {"records": [{"fields": {...}}], "typecast": false} -> {"records": [...]}
    GET  /v0/{base}/{table}                   {"records": [...]}
    GET  /v0/{base}/{table}/{record_id}       one record (formula fields computed)
    POST /admin/rename                        {"table": "Applications", "from": "Company", "to": "Employer"}
    GET  /admin/records?table=Applications    [{<field>: value, ..., "_id": "rec...", "_createdTime": iso}]
    POST /admin/reset                         records cleared, fields back to the seed

Seeded base BASE_ID, table "Applications": Job Title (singleLineText), Company (singleLineText), Link (url),
Date Applied (date), Email (email) and Summary (a formula: computed, so not in the contract).
`create_app(fields=REAL_BASE_FIELDS)` seeds the same table without Email, like the real demo base.
"""

from __future__ import annotations

import copy
import itertools
import re
import threading
from datetime import date, datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

BASE_ID = "appPatchworkDemo1"
TABLE_ID = "tblApplications01"
TABLE = "Applications"
TOKEN = "patFakeAirtable.0123456789abcdef"

SEED_FIELDS: list[dict[str, Any]] = [
    {"id": "fldJobTitle00001", "name": "Job Title", "type": "singleLineText"},
    {"id": "fldCompany000001", "name": "Company", "type": "singleLineText"},
    {"id": "fldLink000000001", "name": "Link", "type": "url"},
    {"id": "fldDateApplied01", "name": "Date Applied", "type": "date",
     "options": {"dateFormat": {"name": "iso", "format": "YYYY-MM-DD"}}},
    {"id": "fldEmail00000001", "name": "Email", "type": "email"},
    {"id": "fldSummary000001", "name": "Summary", "type": "formula", "description": "Job Title at Company",
     "options": {"formula": "{fldJobTitle00001} & \" at \" & {fldCompany000001}", "isValid": True,
                 "referencedFieldIds": ["fldJobTitle00001", "fldCompany000001"], "result": {"type": "singleLineText"}}},
]
# Like the lead's real demo base (2026-11-14): Job Title, Company, Link, Date Applied and NO Email field, so
# retargeting fixture-jobs (which fills an Email question) makes TEACH ask about Email. Plus the formula field.
REAL_BASE_FIELDS: list[dict[str, Any]] = [f for f in SEED_FIELDS if f["name"] != "Email"]
COMPUTED = {"formula", "rollup", "lookup", "multipleLookupValues", "count", "autoNumber", "createdTime",
            "lastModifiedTime", "createdBy", "lastModifiedBy", "button"}
_TEXT = {"singleLineText", "multilineText", "richText", "phoneNumber", "email", "url"}
_NUMBER = {"number", "currency", "percent", "rating", "duration"}


def _err(status: int, type_: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"type": type_, "message": message}}, status_code=status)


class _Invalid(Exception):
    def __init__(self, type_: str, message: str):
        self.type, self.message = type_, message


def _check_value(field: dict, value: Any, typecast: bool) -> Any:
    t, name = field["type"], field["name"]
    bad = _Invalid("INVALID_VALUE_FOR_COLUMN", f'Field "{name}" cannot accept the provided value')
    if value is None:
        return None
    if t in COMPUTED:
        raise _Invalid("INVALID_VALUE_FOR_COLUMN", f'Field "{name}" cannot accept a value because the field is computed')
    if t in _TEXT:
        if not isinstance(value, str):
            if typecast and isinstance(value, (int, float)):
                return str(value)
            raise bad
        return value
    if t in _NUMBER:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            if typecast and isinstance(value, str):
                try:
                    return float(value)
                except ValueError:
                    raise bad from None
            raise bad
        return value
    if t == "checkbox":
        if not isinstance(value, bool):
            raise bad
        return value
    if t == "date":
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise bad
        try:
            date.fromisoformat(value)
        except ValueError:
            raise bad from None
        return value
    if t == "dateTime":
        try:
            datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            raise bad from None
        return value
    if t in ("singleSelect", "multipleSelects"):
        choices = field.setdefault("options", {}).setdefault("choices", [])
        values = value if t == "multipleSelects" else [value]
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise bad
        for v in values:
            if v not in [c["name"] for c in choices]:
                if not typecast:
                    raise _Invalid("INVALID_MULTIPLE_CHOICE_OPTIONS", f'Insufficient permissions to create new select option ""{v}""')
                choices.append({"id": f"sel{len(choices):012d}", "name": v})
        return value
    raise bad


def create_app(token: str = TOKEN, fields: list[dict[str, Any]] | None = None) -> FastAPI:
    """`fields`: the Applications table's fields (default SEED_FIELDS; REAL_BASE_FIELDS mirrors the real base).
    /admin/reset restores them."""
    seed = copy.deepcopy(fields if fields is not None else SEED_FIELDS)
    app = FastAPI(title="Fake Airtable")
    lock = threading.Lock()
    ids = itertools.count(1)
    st: dict[str, Any] = {}

    def reset() -> None:
        st["bases"] = {BASE_ID: {"tables": [{"id": TABLE_ID, "name": TABLE, "primaryFieldId": "fldJobTitle00001",
                                              "fields": copy.deepcopy(seed), "records": {}}]}}

    reset()

    def table_of(base_id: str, table: str) -> dict | None:
        base = st["bases"].get(base_id)
        if base is None:
            return None
        return next((t for t in base["tables"] if t["id"] == table), None) or \
            next((t for t in base["tables"] if t["name"] == table), None)

    def authorized(request: Request) -> bool:
        return request.headers.get("authorization") == f"Bearer {token}"

    def unauthorized() -> JSONResponse:
        return _err(401, "AUTHENTICATION_REQUIRED", "Authentication required")

    def computed(t: dict, rec: dict) -> dict:
        out = dict(rec["fields"])
        by_id = {f["id"]: f for f in t["fields"]}
        for f in t["fields"]:
            if f["type"] == "formula":
                refs = [by_id.get(i) for i in f["options"].get("referencedFieldIds", [])]
                vals = [rec["fields"].get(r["name"], "") if r else "" for r in refs]
                if any(vals):
                    out[f["name"]] = " at ".join(str(v) for v in vals)
        return {"id": rec["id"], "createdTime": rec["createdTime"], "fields": out}

    @app.get("/v0/meta/bases/{base_id}/tables")
    def meta_tables(base_id: str, request: Request):
        if not authorized(request):
            return unauthorized()
        base = st["bases"].get(base_id)
        if base is None:
            return _err(404, "NOT_FOUND", f"Could not find base {base_id}")
        return {"tables": [{k: copy.deepcopy(v) for k, v in t.items() if k != "records"} for t in base["tables"]]}

    @app.post("/v0/{base_id}/{table}")
    async def create(base_id: str, table: str, request: Request):
        if not authorized(request):
            return unauthorized()
        t = table_of(base_id, table)
        if t is None:
            return _err(404, "TABLE_NOT_FOUND", f"Could not find table {table} in base {base_id}")
        try:
            body = await request.json()
        except ValueError:
            return _err(422, "INVALID_REQUEST_UNKNOWN", "Invalid request: parameter validation failed")
        records = body.get("records") if isinstance(body, dict) else None
        if not isinstance(records, list) or not records or len(records) > 10:
            return _err(422, "INVALID_RECORDS", "Invalid request: records must be a list of 1 to 10 records")
        typecast = bool(body.get("typecast"))
        with lock:
            by_name = {f["name"]: f for f in t["fields"]}
            created = []
            for r in records:
                fields = (r or {}).get("fields")
                if not isinstance(fields, dict):
                    return _err(422, "INVALID_REQUEST_MISSING_FIELDS", "Could not find field \"fields\" in the request body")
                clean = {}
                for name, value in fields.items():
                    if name not in by_name:
                        return _err(422, "UNKNOWN_FIELD_NAME", f'Unknown field name: "{name}"')
                    try:
                        v = _check_value(by_name[name], value, typecast)
                    except _Invalid as e:
                        return _err(422, e.type, e.message)
                    if v is not None and v != "":
                        clean[name] = v
                created.append(clean)
            out = []
            for clean in created:
                rid = f"rec{next(ids):014d}"
                rec = {"id": rid, "createdTime": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                       "fields": clean}
                t["records"][rid] = rec
                out.append(computed(t, rec))
        return {"records": out}

    @app.get("/v0/{base_id}/{table}")
    def list_records(base_id: str, table: str, request: Request):
        if not authorized(request):
            return unauthorized()
        t = table_of(base_id, table)
        if t is None:
            return _err(404, "TABLE_NOT_FOUND", f"Could not find table {table} in base {base_id}")
        return {"records": [computed(t, r) for r in t["records"].values()]}

    @app.get("/v0/{base_id}/{table}/{record_id}")
    def get_record(base_id: str, table: str, record_id: str, request: Request):
        if not authorized(request):
            return unauthorized()
        t = table_of(base_id, table)
        if t is None:
            return _err(404, "TABLE_NOT_FOUND", f"Could not find table {table} in base {base_id}")
        rec = t["records"].get(record_id)
        if rec is None:
            return _err(404, "NOT_FOUND", f"Could not find record {record_id}")
        return computed(t, rec)

    @app.post("/admin/rename")
    async def rename(request: Request):
        b = await request.json()
        t = table_of(b.get("base_id") or BASE_ID, b.get("table") or TABLE)
        if t is None:
            return _err(404, "TABLE_NOT_FOUND", "no such table")
        old, new = b.get("from"), b.get("to")
        with lock:
            f = next((f for f in t["fields"] if f["name"] == old), None)
            if f is None:
                return _err(404, "FIELD_NOT_FOUND", f"no field {old!r}")
            if any(x["name"] == new for x in t["fields"]):
                return _err(422, "DUPLICATE_FIELD_NAME", f"a field {new!r} already exists")
            f["name"] = new
            for rec in t["records"].values():
                if old in rec["fields"]:
                    rec["fields"][new] = rec["fields"].pop(old)
        return {"ok": True, "fields": [x["name"] for x in t["fields"]]}

    @app.get("/admin/records")
    def admin_records(table: str = TABLE, base_id: str = BASE_ID):
        t = table_of(base_id, table)
        if t is None:
            return _err(404, "TABLE_NOT_FOUND", "no such table")
        return [{**computed(t, r)["fields"], "_id": r["id"], "_createdTime": r["createdTime"]} for r in t["records"].values()]

    @app.post("/admin/reset")
    def admin_reset():
        with lock:
            reset()
        return {"ok": True}

    return app
