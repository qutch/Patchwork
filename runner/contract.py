"""Contract checks against a destination's /openapi.json.

Deliberately small: resolves local $refs and checks required, type, enum,
exclusiveMinimum/minimum and additionalProperties. That's enough to catch the
renames, retypes and new required fields a real contract change brings.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

_TYPES = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "null": lambda v: v is None,
}


def openapi_hash(doc: dict[str, Any]) -> str:
    canon = json.dumps(doc, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()


def request_schema(doc: dict[str, Any], method: str, path: str) -> dict[str, Any] | None:
    op = doc.get("paths", {}).get(path, {}).get(method.lower())
    if op is None:
        return None
    return (op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")) or {}


def _resolve(doc: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    seen = 0
    while "$ref" in schema and seen < 20:
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            return {}
        node: Any = doc
        for part in ref[2:].split("/"):
            node = node.get(part, {})
        schema, seen = node, seen + 1
    return schema


def _check(doc: dict, schema: dict, value: Any, where: str, errors: list[str]) -> None:
    schema = _resolve(doc, schema)
    if "anyOf" in schema or "oneOf" in schema:
        options = schema.get("anyOf") or schema.get("oneOf")
        if not any(not _errors(doc, opt, value, where) for opt in options):
            errors.append(f"{where}: {value!r} matches none of the allowed shapes")
        return
    if "allOf" in schema:
        for sub in schema["allOf"]:
            _check(doc, sub, value, where, errors)
    t = schema.get("type")
    if t and not _TYPES.get(t, lambda v: True)(value):
        errors.append(f"{where}: expected {t}, got {type(value).__name__}")
        return
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{where}: {value!r} not in enum {schema['enum']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "exclusiveMinimum" in schema and not value > schema["exclusiveMinimum"]:
            errors.append(f"{where}: must be > {schema['exclusiveMinimum']}")
        if "minimum" in schema and not value >= schema["minimum"]:
            errors.append(f"{where}: must be >= {schema['minimum']}")
    if t == "object" or "properties" in schema:
        if not isinstance(value, dict):
            errors.append(f"{where}: expected object")
            return
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{where}.{req}: required by destination but missing from body")
        for k, v in value.items():
            if k in props:
                _check(doc, props[k], v, f"{where}.{k}", errors)
            elif schema.get("additionalProperties") is False:
                errors.append(f"{where}.{k}: not accepted by destination (no such property)")


def _errors(doc: dict, schema: dict, value: Any, where: str) -> list[str]:
    out: list[str] = []
    _check(doc, schema, value, where, out)
    return out


def validate_body(doc: dict[str, Any], method: str, path: str, body: Any) -> list[str]:
    """Return contract errors for sending `body` to `method path`. Empty list means OK."""
    schema = request_schema(doc, method, path)
    if schema is None:
        return [f"{method} {path}: operation not found in destination OpenAPI"]
    return _errors(doc, schema, body, "body")
