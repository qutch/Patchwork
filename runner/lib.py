"""Runtime helpers imported by generated integration code.

Rule: nothing in runner/ may import an LLM SDK (enforced by tests/test_no_ai.py and the
generated no-AI test in every compiled integration).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import quote

AI_MODULES = ("langchain", "langchain_core", "langchain_anthropic", "langgraph", "deepagents",
              "anthropic", "openai")


class Quarantine(Exception):
    """A record can't be processed safely. `code` is one of spec.QUARANTINE_CODES."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


# ---- extraction -------------------------------------------------------------------

def extract_regex(text: str, pattern: re.Pattern[str], where: str) -> str:
    m = pattern.search(text or "")
    if m is None:
        raise Quarantine("PARSE_NO_MATCH", f"{where}: /{pattern.pattern}/ did not match")
    return m.group(1).strip()


def extract_between(text: str, left: str, right: str, where: str) -> str:
    text = text or ""
    i = text.find(left)
    if i < 0:
        raise Quarantine("PARSE_NO_MATCH", f"{where}: left marker {left!r} not found")
    start = i + len(left)
    j = text.find(right, start) if right else len(text)
    if j < 0:
        raise Quarantine("PARSE_NO_MATCH", f"{where}: right marker {right!r} not found")
    return text[start:j].strip()


_JSON_PATH_TOKEN = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]")


def extract_json_path(obj: Any, path: str, where: str) -> Any:
    """Minimal JSONPath: $.a.b[0].c"""
    cur = obj
    for name, idx in _JSON_PATH_TOKEN.findall(path[1:]):
        try:
            cur = cur[int(idx)] if idx else cur[name]
        except (KeyError, IndexError, TypeError):
            raise Quarantine("PARSE_NO_MATCH", f"{where}: {path} not present") from None
    return cur


def cast(value: Any, to: str, where: str) -> Any:
    if to == "str":
        return value if isinstance(value, str) else str(value)
    try:
        if to == "int":
            return int(str(value).replace(",", "").strip())
        if to == "float":
            return float(str(value).replace(",", "").strip())
    except ValueError:
        raise Quarantine("PARSE_NO_MATCH", f"{where}: cannot cast {value!r} to {to}") from None
    raise ValueError(f"unknown cast {to}")


# ---- validation -------------------------------------------------------------------

def check_required(fields: dict, name: str) -> None:
    if fields.get(name) in (None, ""):
        raise Quarantine("VALIDATION_FAILED", f"{name}: required")


def check_positive_int(fields: dict, name: str) -> None:
    v = fields.get(name)
    if not (isinstance(v, int) and not isinstance(v, bool) and v > 0):
        raise Quarantine("VALIDATION_FAILED", f"{name}: expected positive int, got {v!r}")


def check_regex(fields: dict, name: str, pattern: str) -> None:
    if not re.fullmatch(pattern, str(fields.get(name, ""))):
        raise Quarantine("VALIDATION_FAILED", f"{name}: does not match /{pattern}/")


def check_one_of(fields: dict, name: str, values: list) -> None:
    if fields.get(name) not in values:
        raise Quarantine("VALIDATION_FAILED", f"{name}: {fields.get(name)!r} not in {values}")


def check_exists(http, target: str, path: str, params: dict, fields: dict, record: dict, where: str) -> None:
    resp = http.request(target, "GET", render(path, fields, record, url=True),
                        params={k: render(v, fields, record) for k, v in params.items()} or None)
    if resp.status_code == 404:
        raise Quarantine("NOT_FOUND", f"{where}: {render(path, fields, record)} returned 404")
    if resp.status_code in (401, 403):
        raise Quarantine("AUTH", f"{where}: existence check returned {resp.status_code}")
    if resp.status_code >= 300:
        raise Quarantine("UNKNOWN", f"{where}: existence check returned {resp.status_code}")


# ---- templating -------------------------------------------------------------------

_REF = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.]*)\}")


def lookup_ref(ref: str, fields: dict, record: dict) -> Any:
    if ref.startswith("record."):
        key = ref.removeprefix("record.")
        if key not in record:
            raise Quarantine("PARSE_NO_MATCH", f"record has no field {key!r}")
        return record[key]
    if ref not in fields:
        raise Quarantine("PARSE_NO_MATCH", f"no extracted field {ref!r}")
    return fields[ref]


def render(template: str, fields: dict, record: dict, *, url: bool = False) -> str:
    def sub(m: re.Match[str]) -> str:
        v = str(lookup_ref(m.group(1), fields, record))
        return quote(v, safe="") if url else v
    return _REF.sub(sub, template)


# ---- built-in transforms (closed set; see spec.models.BUILTIN_TRANSFORMS) ------------

class T:
    @staticmethod
    def int(v: Any) -> int:
        return cast(v, "int", "transform:int")

    @staticmethod
    def float(v: Any) -> float:
        return cast(v, "float", "transform:float")

    @staticmethod
    def strip(v: Any) -> str:
        return str(v).strip()

    @staticmethod
    def upper(v: Any) -> str:
        return str(v).upper()

    @staticmethod
    def lower(v: Any) -> str:
        return str(v).lower()

    @staticmethod
    def date(v: Any, fmt_in: str, fmt_out: str) -> str:
        try:
            return datetime.strptime(str(v).strip(), fmt_in).strftime(fmt_out)
        except ValueError:
            raise Quarantine("VALIDATION_FAILED", f"date {v!r} does not match {fmt_in}") from None

    @staticmethod
    def lookup(v: Any, table: dict, default: Any = None) -> Any:
        if v in table:
            return table[v]
        if default is not None:
            return default
        raise Quarantine("VALIDATION_FAILED", f"lookup: no entry for {v!r}")

    @staticmethod
    def multiply(v: Any, by: float) -> Any:
        out = cast(v, "float", "transform:multiply") * by
        return int(out) if float(out).is_integer() else out
