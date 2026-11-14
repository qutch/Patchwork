"""Per-record pipeline: route parser -> extract -> validate -> dedupe -> map -> sink -> verify
-> mark processed. Every record ends as success, duplicate or quarantined(reason).

contracts V18: a package with `EMITS = "rows"` goes through `process_document` instead, which turns one
document into one Outcome per row (or one Outcome for the document when it is held as a whole)."""

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


# contracts V18: a flagged document is held with a plain reason and never parsed (nor sent to LEARN).
FLAG_REASONS = {
    "no_text_layer": "no text layer: a scanned or image-only PDF; Patchwork reads text PDFs only",
    "encrypted": "encrypted: the document is password-protected; Patchwork cannot read it",
    "unreadable": "unreadable: the document could not be read",
}


def _hold(out: Outcome, e: Exception) -> Outcome:
    out.status = "quarantined"
    if isinstance(e, Quarantine):
        out.reason, out.detail = e.code, e.detail
    elif isinstance(e, httpx.HTTPError):
        out.reason, out.detail = "UNKNOWN", f"network: {e!r}"
    else:
        out.reason, out.detail = "UNKNOWN", f"{type(e).__name__}: {e}"
    return out


def _redacted(outs: list[Outcome]) -> list[Outcome]:
    for out in outs:
        if out.detail:
            out.detail = redact(out.detail)  # V13
    return outs


def process_document(integ: Any, record: dict, http: Any, store: ProcessedStore) -> list[Outcome]:
    """contracts V18: one document -> one Outcome per row (`<doc id>#<row_no>`), or one Outcome with the
    document's id when the document as a whole is held (a flag, no parser, a parse failure, a failed cross-check,
    or, with `hold: document`, any row that fails validation or body building). With `hold: document` every row
    is validated and its body built before the first write; a replay re-sends only rows not yet processed."""
    doc_id = str(record.get(integ.RECORD_ID))
    doc = Outcome(record_id=doc_id, status="quarantined",
                  route_value=str(record.get(integ.ROUTE_BY)) if integ.ROUTE_BY else None)
    flags = [str(f) for f in (record.get("flags") or [])]
    if flags:
        reasons = [FLAG_REASONS.get(f, f"flagged {f}") for f in flags]
        error = (record.get("meta") or {}).get("error") if isinstance(record.get("meta"), dict) else None
        doc.reason = "PARSE_NO_MATCH"
        doc.detail = "; ".join(reasons) + (f" ({error})" if error else "")
        return _redacted([doc])
    try:
        doc.parser, fields = _extract(integ, record, doc)
        fields = dict(fields)
        rows = fields.pop("__rows__")
        integ.validate_document(fields, rows)
    except Quarantine as q:
        _hold(doc, q)
        kept, doc_fields = getattr(q, "rows", None), getattr(q, "fields", None)
        if kept and doc_fields is not None:  # an unexplained skipped row: a failed cross-check says more
            try:
                integ.validate_document(doc_fields, kept)
            except Quarantine as cross:
                doc.reason, doc.detail = cross.code, f"{cross.detail}; and {q.detail}"
        return _redacted([doc])
    except Exception as e:  # noqa: BLE001 - every document must end in a known state
        return _redacted([_hold(doc, e)])
    hold = getattr(integ, "HOLDS", {}).get(doc.parser, "document")
    prepared: list[tuple[Outcome, dict, dict]] = []
    keys: dict[str, int] = {}
    for row in rows:
        rec_row = {**record, "row_no": row.no, "row_count": len(rows)}
        merged = {**fields, **row.fields}
        out = Outcome(record_id=f"{doc_id}#{row.no}", status="quarantined", parser=doc.parser,
                      route_value=doc.route_value, routed_by=doc.routed_by)
        try:
            integ.validate(merged, rec_row, http)
            out.key = integ.dedupe_key(merged, rec_row)
            if out.key in keys:
                raise Quarantine("VALIDATION_FAILED", f"rows {keys[out.key]} and {row.no} have the same dedupe key "
                                                      f"{out.key!r}; the dedupe key must tell the rows apart")
            keys[out.key] = row.no
            if store.is_processed(out.key):
                out.status = "duplicate"
            else:
                out.body = integ.build_body(merged, rec_row)
                out.status = "ready"
        except Exception as e:  # noqa: BLE001
            _hold(out, e)
            if hold == "document":
                doc.reason, doc.detail = out.reason, f"row {row.no}: {out.detail}"
                return _redacted([doc])
        prepared.append((out, merged, rec_row))
    for out, merged, rec_row in prepared:
        if out.status != "ready":
            continue
        try:
            resp = integ.send(http, out.body)
            _sink_status(resp.status_code, resp.text, http)
            integ.verify(http, merged, rec_row)
            store.mark(out.key)
            out.status = "success"
            out.detail = getattr(resp, "detail", None)
        except Exception as e:  # noqa: BLE001
            _hold(out, e)
    return _redacted([out for out, _, _ in prepared])
