"""Per-record pipeline: route parser -> extract -> validate -> dedupe -> map -> sink -> verify
-> mark processed. Every record ends as success, duplicate or quarantined(reason)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol

import httpx

from runner.credentials import redact
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
    routed_by: str | None = None  # v2: "match" | "fingerprint" | "default" | None (contracts V4)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sink_status(status: int, resp_text: str, http: Any = None) -> None:
    if status < 300:
        return
    detail = f"sink returned {status}: {redact(resp_text[:300])}"
    if status in (400, 422):
        raise Quarantine("SINK_CONTRACT", detail)
    if status in (401, 403):
        cred = getattr(http, "sink_credential", None)
        if cred:  # contracts V13: name the credential, never show it (nor the body, which may echo it)
            raise Quarantine("AUTH", f"{getattr(http, 'sink_label', 'the destination')} rejected the credential "
                                     f"{cred} (HTTP {status})")
        raise Quarantine("AUTH", detail)
    raise Quarantine("UNKNOWN", detail)


def _extract(integ: Any, record: dict, out: Outcome) -> tuple[str, dict]:
    """Route then parse. Packages compiled by the v2 templates (PIPELINE_API >= 2) expose
    route() -> (parser_id, fn, routed_by), so routed_by is known even when the parser fails.
    Older packages only have extract() -> (parser_id, fields) (or a 3-tuple with routed_by)."""
    if getattr(integ, "PIPELINE_API", 1) >= 2:
        parser_id, parse, out.routed_by = integ.route(record)
        try:
            return parser_id, parse(record)
        except Quarantine as q:
            if out.routed_by == "fingerprint":
                q.detail = f"routed to {parser_id} by fingerprint (site {record.get(integ.ROUTE_BY)!r}): {q.detail}"
            raise
    res = integ.extract(record)
    if len(res) > 2:
        out.routed_by = res[2]
    return res[0], res[1]


def process_record(integ: Any, record: dict, http: Any, store: ProcessedStore) -> Outcome:
    out = Outcome(record_id=str(record.get(integ.RECORD_ID)), status="quarantined",
                  route_value=str(record.get(integ.ROUTE_BY)) if integ.ROUTE_BY else None)
    try:
        out.parser, fields = _extract(integ, record, out)
        integ.validate(fields, record, http)
        out.key = integ.dedupe_key(fields, record)
        if store.is_processed(out.key):
            out.status = "duplicate"
            return out
        out.body = integ.build_body(fields, record)
        resp = integ.send(http, out.body)
        _sink_status(resp.status_code, resp.text, http)
        integ.verify(http, fields, record)
        store.mark(out.key)
        out.status = "success"
        out.detail = getattr(resp, "detail", None)  # form sinks: "dry run: filled, not submitted"
    except Quarantine as q:
        out.status, out.reason, out.detail = "quarantined", q.code, q.detail
    except httpx.HTTPError as e:
        out.status, out.reason, out.detail = "quarantined", "UNKNOWN", f"network: {e!r}"
    except Exception as e:  # noqa: BLE001 - every record must end in a known state
        out.status, out.reason, out.detail = "quarantined", "UNKNOWN", f"{type(e).__name__}: {e}"
    if out.detail:
        out.detail = redact(out.detail)  # V13: nothing that leaves the runner shows a credential value
    return out
