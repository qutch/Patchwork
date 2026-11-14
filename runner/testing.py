"""Test doubles used by generated tests. No network, no LLM."""

from __future__ import annotations

import json as jsonlib
from typing import Any
from urllib.parse import unquote


class FakeResponse:
    def __init__(self, status_code: int, data: Any = None):
        self.status_code = status_code
        self._data = data
        self.text = jsonlib.dumps(data) if data is not None else ""

    def json(self) -> Any:
        return self._data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    """Source returns `records`; sink accepts writes and answers GETs.

    A GET with query params is treated as a lookup over written rows (verify); a GET
    without params is an existence check that 404s when any path segment is in `missing`.
    """

    def __init__(self, records: list[dict] | None = None, missing: list[str] | None = None,
                 sink_status: int = 201):
        self.records = records or []
        self.missing = set(missing or [])
        self.sink_status = sink_status
        self.calls: list[tuple[str, str, str, Any]] = []
        self.rows: list[dict] = []

    @property
    def sink_writes(self) -> list[dict]:
        return [c[3] for c in self.calls if c[0] == "sink" and c[1] in ("POST", "PUT", "PATCH")]

    # -- form sinks (contracts V5): the same bookkeeping as an API write to POST /form ------------
    dry_run = False

    def form_submit(self, url: str, values: dict, *, submit: str = "Submit",
                    confirmation_text: str | None = None) -> FakeResponse:
        self.calls.append(("sink", "POST", "/form", dict(values)))
        if self.sink_status < 300:
            self.rows.append(dict(values))
        return FakeResponse(200 if self.sink_status < 300 else self.sink_status, {"ok": self.sink_status < 300})

    def form_contract(self, url: str) -> dict:
        raise RuntimeError("FakeHttp has no form contract; tests read contract/openapi.json")

    # -- Airtable sinks (contracts V14): the same bookkeeping as an API write to POST /records --------
    def airtable_submit(self, base_id: str, table: str, values: dict) -> FakeResponse:
        self.calls.append(("sink", "POST", "/records", dict(values)))
        if self.sink_status < 300:
            self.rows.append(dict(values))
        return FakeResponse(200 if self.sink_status < 300 else self.sink_status,
                            {"id": f"recFAKE{len(self.rows):010d}"} if self.sink_status < 300 else
                            {"error": {"type": "INVALID_REQUEST", "message": "rejected by FakeHttp"}})

    def airtable_contract(self, base_id: str, table: str) -> dict:
        raise RuntimeError("FakeHttp has no Airtable contract; tests read contract/openapi.json")

    # -- SQLite sinks (contracts V16): the same bookkeeping as an API write to POST /rows ---------------
    def sqlite_submit(self, path: str, table: str, values: dict) -> FakeResponse:
        self.calls.append(("sink", "POST", "/rows", dict(values)))
        if self.sink_status < 300:
            self.rows.append(dict(values))
        return FakeResponse(200 if self.sink_status < 300 else self.sink_status,
                            {"id": len(self.rows)} if self.sink_status < 300 else {"error": "rejected by FakeHttp"})

    def sqlite_contract(self, path: str, table: str) -> dict:
        raise RuntimeError("FakeHttp has no SQLite contract; tests read contract/openapi.json")

    def request(self, target: str, method: str, path: str, *, params: dict | None = None,
                json: Any = None) -> FakeResponse:
        self.calls.append((target, method, path, json if json is not None else params))
        if target == "source":
            return FakeResponse(200, list(self.records))
        if method in ("POST", "PUT", "PATCH"):
            if self.sink_status < 300:
                self.rows.append(dict(json or {}))
            return FakeResponse(self.sink_status, {"ok": self.sink_status < 300})
        if params:
            wanted = {str(v) for v in params.values()}
            return FakeResponse(200, [r for r in self.rows if wanted <= {str(v) for v in r.values()}])
        segments = {unquote(s) for s in path.split("/")}
        if segments & self.missing:
            return FakeResponse(404, {"detail": "not found"})
        return FakeResponse(200, {})
