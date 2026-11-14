"""Per-record pipeline: route parser -> extract -> validate -> dedupe -> map -> sink -> verify
-> mark processed. Every record ends as success, duplicate or quarantined(reason)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol

import httpx

from runner.lib import Quarantine


class ProcessedStore(Protocol):
    def is_processed(self, key: str) -> bool: ...
    def mark(self, key: str) -> None: ...


class MemoryStore:
    def __init__(self) -> None:
        self.keys: set[str] = set()

    def is_processed(self, key: str) -> bool:
        return key in self.keys

    def mark(self, key: str) -> None:
        self.keys.add(key)


@dataclass
class Outcome:
    record_id: str
    status: str  # success | duplicate | quarantined
    reason: str | None = None
    detail: str | None = None
    parser: str | None = None
    route_value: str | None = None
    key: str | None = None
    body: dict | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sink_status(status: int, resp_text: str) -> None:
    if status < 300:
        return
    detail = f"sink returned {status}: {resp_text[:300]}"
    if status in (400, 422):
        raise Quarantine("SINK_CONTRACT", detail)
    if status in (401, 403):
        raise Quarantine("AUTH", detail)
    raise Quarantine("UNKNOWN", detail)


def process_record(integ: Any, record: dict, http: Any, store: ProcessedStore) -> Outcome:
    out = Outcome(record_id=str(record.get(integ.RECORD_ID)), status="quarantined",
                  route_value=str(record.get(integ.ROUTE_BY)) if integ.ROUTE_BY else None)
    try:
        out.parser, fields = integ.extract(record)
        integ.validate(fields, record, http)
        out.key = integ.dedupe_key(fields, record)
        if store.is_processed(out.key):
            out.status = "duplicate"
            return out
        out.body = integ.build_body(fields, record)
        resp = integ.send(http, out.body)
        _sink_status(resp.status_code, resp.text)
        integ.verify(http, fields, record)
        store.mark(out.key)
        out.status = "success"
    except Quarantine as q:
        out.status, out.reason, out.detail = "quarantined", q.code, q.detail
    except httpx.HTTPError as e:
        out.status, out.reason, out.detail = "quarantined", "UNKNOWN", f"network: {e!r}"
    except Exception as e:  # noqa: BLE001 - every record must end in a known state
        out.status, out.reason, out.detail = "quarantined", "UNKNOWN", f"{type(e).__name__}: {e}"
    return out
