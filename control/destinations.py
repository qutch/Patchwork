"""Destinations for the new-integration wizard: check a connection and read what it accepts.

`check(dest)` answers "can Patchwork write here, and which fields does it take?" for every kind of
destination the wizard offers (airtable, form, api, sqlite), and raises `DestinationError` with a next-step
message when it can't. `airtable_tables` and `api_operations` feed the pickers. Credentials are only named
here, never shown: errors carry the credential's name and the destination's own words, run through
`control.credentials.redact`.
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from control import credentials as store_creds
from runner import credentials as cred

KINDS = ("airtable", "form", "api", "sqlite")
WRITE_METHODS = ("post", "put", "patch")
BASE_ID_IN_URL = re.compile(r"app[A-Za-z0-9]{14}")
TIMEOUT_S = 10


class DestinationError(Exception):
    """A destination can't be used; the message says what to do next."""


def base_id_from_url(value: str) -> str | None:
    """`https://airtable.com/appXXXXXXXXXXXXXX/tbl.../viw...` (or the bare id) -> `appXXXXXXXXXXXXXX`, else None."""
    m = BASE_ID_IN_URL.search(value or "")
    return m.group(0) if m else None


# ---- validation of the destination dict -----------------------------------------------------------

def normalize(dest: dict[str, Any]) -> dict[str, Any]:
    """The destination in its stored shape (see the design doc), or DestinationError."""
    kind = (dest or {}).get("kind")
    if kind not in KINDS:
        raise DestinationError(f"choose a destination kind: {', '.join(KINDS)}")

    def need(key: str, what: str) -> str:
        v = dest.get(key)
        if not isinstance(v, str) or not v.strip():
            raise DestinationError(f"{kind} destination needs {what}")
        return v.strip()

    if kind == "airtable":
        base = base_id_from_url(dest.get("base_id") or "") or base_id_from_url(dest.get("base_url") or "")
        if not base:
            raise DestinationError("paste the Airtable base URL (it contains appXXXXXXXXXXXXXX) or the base id")
        out = {"kind": "airtable", "base_id": base, "table": need("table", "a table name or id"),
               "credential": need("credential", "the credential to use (its name, e.g. AIRTABLE_TOKEN)")}
    elif kind == "form":
        url = need("url", "the form's URL")
        if not re.match(r"^https?://", url):
            raise DestinationError("the form URL must start with http:// or https://")
        out = {"kind": "form", "url": url}
    elif kind == "sqlite":
        out = {"kind": "sqlite", "path": need("path", "the database file's path"), "table": need("table", "a table name")}
    else:
        base_url = need("base_url", "the API's base URL")
        if not re.match(r"^https?://", base_url):
            raise DestinationError("the API base URL must start with http:// or https://")
        op = dest.get("operation") or {}
        method, path = str(op.get("method") or "").upper(), str(op.get("path") or "")
        if method.lower() not in WRITE_METHODS or not path.startswith("/"):
            raise DestinationError("choose the operation that adds a record (a POST, PUT or PATCH path)")
        out = {"kind": "api", "base_url": base_url.rstrip("/"), "operation": {"method": method, "path": path}}
        if dest.get("credential"):
            out["credential"] = dest["credential"]
    if out.get("credential"):
        try:
            cred.check_name(out["credential"])
        except ValueError as e:
            raise DestinationError(f"credential: {e}") from None
    return out


# ---- reading contracts ----------------------------------------------------------------------------

def _deref(doc: dict, schema: Any, depth: int = 0) -> dict:
    """Follow `$ref: #/components/...` (bounded; cycles stop at depth 8)."""
    while isinstance(schema, dict) and "$ref" in schema and depth < 8:
        node: Any = doc
        for part in str(schema["$ref"]).lstrip("#/").split("/"):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        schema, depth = node, depth + 1
    return schema if isinstance(schema, dict) else {}


def _merge_all_of(doc: dict, schema: dict) -> dict:
    schema = _deref(doc, schema)
    if "allOf" not in schema:
        return schema
    merged: dict[str, Any] = {k: v for k, v in schema.items() if k != "allOf"}
    props, req = dict(merged.get("properties") or {}), list(merged.get("required") or [])
    for part in schema["allOf"]:
        part = _merge_all_of(doc, part)
        props.update(part.get("properties") or {})
        req += part.get("required") or []
        if "type" in part:
            merged.setdefault("type", part["type"])
    return {**merged, "properties": props, "required": req}


def flatten(doc: dict, schema: Any) -> list[dict[str, Any]]:
    """Scalar leaves of a body schema as `[{name: "venue.name", type, required, options?, format?}]`.
    Objects become dotted paths. An array is not flattened: it is listed once as `<path>[]` (the wizard leaves
    it unmapped)."""
    out: list[dict[str, Any]] = []

    def walk(sch: Any, prefix: str, required: bool, depth: int) -> None:
        sch = _merge_all_of(doc, sch)
        props = sch.get("properties")
        if sch.get("type") == "object" or (props and "type" not in sch):
            if not props or depth > 6:
                if prefix:
                    out.append({"name": prefix, "type": "object", "required": required})
                return
            reqs = set(sch.get("required") or [])
            for key, sub in props.items():
                walk(sub, f"{prefix}.{key}" if prefix else key, required and key in reqs, depth + 1)
            return
        if not prefix:
            return
        if sch.get("type") == "array":
            item = _merge_all_of(doc, sch.get("items") or {})
            leaf: dict[str, Any] = {"name": f"{prefix}[]", "type": "array", "required": required}
            if item.get("enum"):
                leaf["options"] = [str(o) for o in item["enum"]]
            out.append(leaf)
            return
        leaf = {"name": prefix, "type": sch.get("type") or "string", "required": required}
        if sch.get("format"):
            leaf["format"] = sch["format"]
        if sch.get("enum"):
            leaf["options"] = [str(o) for o in sch["enum"]]
        out.append(leaf)

    walk(schema, "", True, 0)
    return out


def _body_schema(doc: dict, op: dict) -> dict:
    content = (_deref(doc, op.get("requestBody") or {}).get("content") or {})
    media = content.get("application/json") or next(iter(content.values()), {})
    return _deref(doc, (media or {}).get("schema") or {})


def operations_of(doc: dict) -> list[dict[str, Any]]:
    """The POST, PUT and PATCH operations of an OpenAPI document with their flattened body fields."""
    ops: list[dict[str, Any]] = []
    for path, item in (doc.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method in WRITE_METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            schema = _body_schema(doc, op)
            fields = flatten(doc, schema)
            ops.append({"method": method.upper(), "path": path,
                        "summary": str(op.get("summary") or op.get("operationId") or "")[:200],
                        "fields": [f["name"] for f in fields],
                        "required": [f["name"] for f in fields if f["required"]]})
    return ops


def _fields_of_contract(doc: dict, method: str | None = None, path: str | None = None) -> list[dict[str, Any]]:
    """Fields of the operation `method path` (default: the first write operation) of a contract document."""
    for p, item in (doc.get("paths") or {}).items():
        if path is not None and p != path:
            continue
        for m in WRITE_METHODS:
            if method is not None and m != method.lower():
                continue
            op = (item or {}).get(m)
            if isinstance(op, dict):
                schema = _body_schema(doc, op)
                return flatten(doc, schema)
    return []


# ---- the network ------------------------------------------------------------------------------------

def _headers(credential: str | None) -> dict[str, str]:
    if not credential:
        return {}
    try:
        return cred.headers({"scheme": "bearer", "credential": credential})
    except (ValueError, cred.CredentialError):
        raise DestinationError(f"the credential {credential} is not set: add it in the credential step") from None


def _host(url: str) -> str:
    return urlparse(url).netloc or url


def _safe(text: Any, credential: str | None = None) -> str:
    return store_creds.redact(text) if credential is None else cred.redact(store_creds.redact(text), names=[credential])


def _get_json(url: str, credential: str | None) -> Any:
    try:
        resp = httpx.get(url, headers=_headers(credential) or None, timeout=TIMEOUT_S, follow_redirects=True)
    except httpx.HTTPError:
        raise DestinationError(f"couldn't reach {_host(url)}: check the address, and that it is running") from None
    if resp.status_code in (401, 403):
        raise DestinationError(f"{_host(url)} rejected the credential {credential or '(none was sent)'}: "
                               f"check its value, or choose a credential that has access")
    if resp.status_code >= 400:
        raise DestinationError(f"{_host(url)} answered HTTP {resp.status_code} for {urlparse(url).path or '/'}")
    try:
        return resp.json()
    except ValueError:
        raise DestinationError(f"{_host(url)} did not answer with JSON at {urlparse(url).path or '/'}") from None


def api_operations(base_url: str, credential: str | None = None) -> list[dict[str, Any]]:
    """`[{method, path, summary, fields: [dotted paths], required: [dotted paths]}]` for the write operations
    (POST, PUT, PATCH) of the API at `base_url` (its /openapi.json)."""
    if not re.match(r"^https?://", base_url or ""):
        raise DestinationError("the API base URL must start with http:// or https://")
    doc = _get_json(base_url.rstrip("/") + "/openapi.json", credential)
    if not isinstance(doc, dict) or "paths" not in doc:
        raise DestinationError(f"{_host(base_url)} has no OpenAPI document at /openapi.json")
    return operations_of(doc)


def airtable_tables(base_id: str, credential: str) -> list[dict[str, str]]:
    """`[{id, name}]` for the tables of an Airtable base, read with the named credential."""
    from runner import airtable
    from runner.lib import Quarantine

    base = base_id_from_url(base_id)
    if not base:
        raise DestinationError("paste the Airtable base URL (it contains appXXXXXXXXXXXXXX) or the base id")
    headers = _headers(credential)
    try:
        with httpx.Client(timeout=TIMEOUT_S) as client:
            resp = airtable._request(client, "GET", f"{airtable.api_url()}/meta/bases/{base}/tables", base_id=base,
                                     headers=headers, pacer=airtable.PACER, what="the list of tables",
                                     credential=credential)
        tables = resp.json().get("tables") or []
    except Quarantine as q:
        raise DestinationError(_airtable_message(q.code, q.detail, credential)) from None
    except httpx.HTTPError:
        raise DestinationError("couldn't reach Airtable: check your connection and try again") from None
    return [{"id": t.get("id"), "name": t.get("name")} for t in tables]


# Airtable's token list shows a token's ID (pat + 14 characters). The token itself is that ID, a dot and a secret,
# and Airtable shows it only once, when the token is created or regenerated.
_AIRTABLE_TOKEN_ID = re.compile(r"^pat[A-Za-z0-9]{14}$")


def _airtable_message(code: str, detail: str, credential: str) -> str:
    detail = _safe(detail, credential)
    if code == "AUTH":
        if _AIRTABLE_TOKEN_ID.match((os.environ.get(credential or "") or "").strip()):
            return (f"{detail}. {credential} looks like the token's ID, not the token: copy the full value (it has a "
                    "dot and a long secret after the ID) that Airtable shows when you create or regenerate the token, "
                    "then set it again under Credentials.")
        return f"{detail}. Check the token's value and that it can read this base's schema (scope schema.bases:read)."
    if code == "SINK_CONTRACT":
        return f"{detail}. Check the base and table names."
    return detail


def check(dest: dict[str, Any]) -> dict[str, Any]:
    """`{kind, title, fields: [{name, type, required, options?}], operations?: [...]}` for a destination dict, or
    DestinationError. Nothing is written to the destination."""
    from control import ops

    dest = normalize(dest)
    kind = dest["kind"]
    try:
        if kind == "airtable":
            _headers(dest["credential"])
            spec = {"sink": {"kind": "airtable", "base_id": dest["base_id"], "table": dest["table"],
                             "auth": {"scheme": "bearer", "credential": dest["credential"]}}}
            doc = ops.fetch_contract(spec)
        elif kind == "form":
            doc = ops.fetch_contract({"sink": {"kind": "form", "url": dest["url"]}})
        elif kind == "sqlite":
            doc = ops.fetch_sqlite_contract(dest["path"], dest["table"])
        else:
            doc = _get_json(dest["base_url"] + "/openapi.json", dest.get("credential"))
    except DestinationError:
        raise
    except RuntimeError as e:  # ops.fetch_airtable_contract / fetch_sqlite_contract: "<CODE>: <detail>"
        code, _, detail = str(e).partition(": ")
        raise DestinationError(_airtable_message(code, detail or str(e), dest.get("credential") or "")
                               if kind == "airtable" else _safe(detail or str(e))) from None
    except httpx.HTTPError:
        raise DestinationError(f"couldn't reach {_host(dest.get('url') or dest.get('base_url') or '')}: "
                               f"check the address, and that it is running") from None
    except Exception as e:  # noqa: BLE001 - a form that won't open, a malformed document
        raise DestinationError(f"couldn't read the destination ({type(e).__name__}): {_safe(str(e))[:200]}") from None
    if not isinstance(doc, dict):
        raise DestinationError("the destination did not describe its fields")
    title = str((doc.get("info") or {}).get("title") or dest.get("table") or dest.get("url") or dest.get("base_url"))
    if kind == "api":
        op = dest["operation"]
        ops_list = operations_of(doc)
        if not any(o["method"] == op["method"] and o["path"] == op["path"] for o in ops_list):
            raise DestinationError(f"{op['method']} {op['path']} is not in the API's document: choose one of its operations")
        fields = _fields_of_contract(doc, op["method"], op["path"])
        return {"kind": kind, "title": title, "fields": fields, "operations": ops_list}
    fields = _fields_of_contract(doc)
    if not fields:
        raise DestinationError("the destination has no fields Patchwork can write to")
    return {"kind": kind, "title": title, "fields": fields}
