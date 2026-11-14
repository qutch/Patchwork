"""Stage A of interpretation (plan section 6): explain each value the user sent to the
destination by finding it in what they saw at the source. Pure Python: no network, no LLM.

1. Destination values: the body of the last mutating request (POST/PUT/PATCH), with typed
   input values as a backup when no such request was captured.
2. Source observations: JSON bodies of GET responses from other hosts, plus copied text.
3. Match after normalising case, whitespace and number format (40 == 40.0 == "40").
4. Provenance: a JSON path for whole-field matches, else a text offset plus the label to the
   left of the value, and a candidate regex with the value generalised to its character class.
5. Anything not found is listed as unexplained.
"""

from __future__ import annotations

import re
from itertools import groupby
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from matcher.pages import is_page_form, match_page_form

MUTATING = {"POST", "PUT", "PATCH"}
ID_KEYS = ("id", "_id", "uuid", "key")
NUM_TOKEN = re.compile(r"(?<![\w.-])-?\d(?:[\d,]*\d)?(?:\.\d+)?(?![\w-]|\.\d)")
LABEL_STOP = re.compile(r"[\n|;,.!?()\[\]{}]")


# ---------------------------------------------------------------- helpers
def _path(url: str) -> str:
    return urlsplit(url).path or "/"


def _origin(url: str) -> str | None:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else None


def _as_number(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and re.fullmatch(r"\s*-?\d[\d,]*(\.\d+)?\s*", v):
        try:
            return float(v.replace(",", ""))
        except ValueError:
            return None
    return None


def _norm(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v)).strip().casefold()


def _flatten(body: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """Leaf fields of a request body as (dotted path, value)."""
    if isinstance(body, dict):
        return [kv for k, v in body.items() for kv in _flatten(v, f"{prefix}.{k}" if prefix else str(k))]
    if isinstance(body, list):
        return [kv for i, v in enumerate(body) for kv in _flatten(v, f"{prefix}[{i}]")]
    return [(prefix, body)] if body not in (None, "") else []


def _cls(ch: str) -> str | None:
    return (r"[A-Z]+" if ch.isupper() else r"[a-z]+" if ch.islower() else r"\d+" if ch.isdigit()
            else r"\s+" if ch.isspace() else None)


def char_class(value: str) -> str:
    """'GAD-217' -> '[A-Z]+-\\d+'; '20' -> '\\d+'; '1,200.50' -> '[\\d,]+(?:\\.\\d+)?'."""
    if _as_number(value) is not None:
        int_part = r"\d[\d,]*" if "," in value else r"\d+"
        return ("-?" if value.strip().startswith("-") else "") + int_part + (r"(?:\.\d+)?" if "." in value else "")
    # '-' needs no escape outside a character class; leaving it bare keeps regexes readable.
    return "".join(cls or re.escape("".join(grp)).replace("\\-", "-") for cls, grp in groupby(value, key=_cls))


def left_label(text: str, start: int, max_len: int = 40) -> str:
    """The label to the left of a value: back to the line start or a delimiter, ≤ 4 words."""
    seg = text[max(0, start - max_len):start]
    stops = list(LABEL_STOP.finditer(seg))
    if stops:
        seg = seg[stops[-1].end():]
    words = seg.strip().split()
    return " ".join(words[-4:])


def label_regex(label: str, value: str) -> str:
    # Words with digits in them vary between records ("20 units of item"): generalise them too.
    lab = r"\s+".join(char_class(w) if any(c.isdigit() for c in w) else re.escape(w) for w in label.split())
    return f"{lab}\\s*({char_class(value)})" if lab else f"({char_class(value)})"


def _occurrences(text: str, value: Any) -> list[tuple[int, int]]:
    num = _as_number(value)
    if num is not None:
        return [(m.start(), m.end()) for m in NUM_TOKEN.finditer(text)
                if _as_number(m.group()) == num]
    words = str(value).split()
    if not words:
        return []
    pat = re.compile(r"(?<![\w-])" + r"\s+".join(re.escape(w) for w in words) + r"(?![\w-])", re.I)
    return [(m.start(), m.end()) for m in pat.finditer(text)]


# ---------------------------------------------------------------- recording -> observations
def _net_events(recording: dict) -> list[dict]:
    """Net events with an absolute origin (taken from the last navigation when not recorded)."""
    out, page_origin = [], None
    for ev in recording.get("events", []):
        if ev.get("type") == "framenavigated":
            page_origin = _origin(ev.get("url", "")) or page_origin
        elif ev.get("type") == "net":
            ev = dict(ev)
            ev["origin"] = ev.get("origin") or _origin(ev.get("url", "")) or page_origin
            out.append(ev)
    return out


def _destination(recording: dict, nets: list[dict]) -> tuple[dict | None, list[tuple[str, Any]]]:
    writes = [e for e in nets if e.get("method", "").upper() in MUTATING and isinstance(e.get("req"), (dict, list))]
    ok = [e for e in writes if isinstance(e.get("status"), int) and e["status"] < 400]
    if ok or writes:
        e = (ok or writes)[-1]
        sink = {"base_url": e["origin"], "method": e["method"].upper(), "path": _path(e["url"]), "body": e["req"],
                "status": e.get("status")}
        return sink, _flatten(e["req"])
    # Backup: final typed values per field (no sink request was captured).
    typed: dict[str, Any] = {}
    for ev in recording.get("events", []):
        if ev.get("type") in ("input", "change", "paste") and ev.get("el"):
            el = ev["el"]
            key = el.get("name") or el.get("testid") or el.get("label") or el.get("css")
            typed[key] = ev.get("value")
    return None, [(k, v) for k, v in typed.items() if v not in (None, "")]


def _records(nets: list[dict], sink_origin: str | None) -> list[dict]:
    """Every JSON object seen in a GET response from a non-sink host."""
    out = []
    for i, e in enumerate(nets):
        if e.get("method", "").upper() != "GET" or e.get("origin") == sink_origin:
            continue
        if not (isinstance(e.get("status"), int) and e["status"] < 400):
            continue
        resp = e.get("resp")
        items = [(f"$[{j}]", r) for j, r in enumerate(resp)] if isinstance(resp, list) else [("$", resp)]
        for path, rec in items:
            if isinstance(rec, dict):
                out.append({"net": e, "index": i, "path": path, "record": rec, "detail": isinstance(resp, dict)})
    return out


def _record_id_key(rec: dict) -> str | None:
    for k in ID_KEYS:
        if k in rec:
            return k
    return next((k for k in rec if k.endswith("_id") and isinstance(rec[k], (str, int))), None)


def _explain(rec: dict, value: Any) -> list[dict]:
    """All ways `value` appears in one source record, best first."""
    found = []
    num = _as_number(value)
    for field, fv in rec.items():
        if isinstance(fv, (dict, list)) or fv is None:
            continue
        if (num is not None and _as_number(fv) == num) or (num is None and _norm(fv) == _norm(value)):
            found.append({"score": 10, "kind": "json", "json_path": f"$.{field}", "source_field": field})
            continue
        if not isinstance(fv, str):
            continue
        for start, end in _occurrences(fv, value):
            label = left_label(fv, start)
            score = 1 + (3 if label[-1:] in (":", "=", "#") else 1 if label else 0) + min(len(fv), 400) / 400
            found.append({"score": score, "kind": "text", "source_field": field, "offset": [start, end],
                          "label": label, "matched": fv[start:end],
                          "candidate_regex": label_regex(label, fv[start:end])})
    found.sort(key=lambda f: -f["score"])
    return found


def match(recording: dict, base_dir: str | Path | None = None) -> dict:
    """Recording (fixtures/recording.json shape) -> candidates (docs/contracts.md section 2).

    A page -> form recording (no mutating request; a submit click carrying form questions) gets V11
    candidates from matcher.pages; `base_dir` is the recording directory holding its page snapshots."""
    nets = _net_events(recording)
    sink, dest = _destination(recording, nets)
    if sink is None and is_page_form(recording):
        return match_page_form(recording, Path(base_dir) if base_dir is not None else None)
    records = _records(nets, sink["base_url"] if sink else None)
    copied = [e.get("text", "") for e in recording.get("events", []) if e.get("type") == "copy" and e.get("text")]

    # The source record is the one that explains the most destination values (detail fetch wins ties).
    best, best_key = None, (-1, False, -1)
    for r in records:
        n = sum(1 for _, v in dest if _explain(r["record"], v))
        key = (n, r["detail"], r["index"])
        if key > best_key:
            best, best_key = r, key
    if best is not None and best_key[0] == 0:
        best = None

    mappings, unexplained = [], []
    for field, value in dest:
        how = _explain(best["record"], value) if best else []
        num = _as_number(value)
        cast = ("int" if float(num).is_integer() else "float") if num is not None and not isinstance(value, str) else None
        was_copied = any(_norm(c) == _norm(value) for c in copied)
        if not how:
            hit = next((c for c in copied if _occurrences(c, value)), None)
            if hit is not None:
                mappings.append({"dest_field": field, "value": value, "kind": "copy", "text": hit, "cast": cast,
                                 "copied": True})
            else:
                unexplained.append({"dest_field": field, "value": value})
            continue
        top = {k: v for k, v in how[0].items() if k != "score"}
        m = {"dest_field": field, "value": value, **top, "cast": cast}
        if top["kind"] == "text":
            rx = re.compile(top["candidate_regex"])
            hit = rx.search(best["record"][top["source_field"]])
            m["regex_ok"] = bool(hit) and _occurrences(hit.group(1), value) != []
        if was_copied:
            m["copied"] = True
        alts = [{k: v for k, v in a.items() if k in ("kind", "json_path", "source_field", "offset", "label")}
                for a in how[1:4]]
        if alts:
            m["alternatives"] = alts
        mappings.append(m)

    source = None
    if best is not None:
        e = best["net"]
        rec = best["record"]
        id_key = _record_id_key(rec)
        texts = {m["source_field"] for m in mappings if m.get("kind") == "text"}
        text_field = max(texts, key=lambda f: len(str(rec.get(f, "")))) if texts else None
        source = {"base_url": e["origin"], "list": _list_endpoint(nets, e, rec, id_key),
                  "record_id": id_key, "text_field": text_field, "record": rec}
        if best["detail"]:
            p = _path(e["url"])
            rid = str(rec.get(id_key)) if id_key else None
            source["detail"] = {"method": "GET", "path": p.replace(rid, "{id}") if rid and rid in p else p}

    return {"recording_id": recording.get("id"), "intent": recording.get("intent"), "sink": sink,
            "source": source, "mappings": mappings, "unexplained": unexplained}


def _list_endpoint(nets: list[dict], e: dict, rec: dict, id_key: str | None) -> dict:
    """The GET that lists source records: one whose response array holds this record, else a
    GET on a parent path of the detail URL, else the detail path minus its id segment."""
    rid = rec.get(id_key) if id_key else None
    for n in nets:
        if n.get("method", "").upper() == "GET" and n.get("origin") == e["origin"] and isinstance(n.get("resp"), list):
            if any(isinstance(r, dict) and id_key and r.get(id_key) == rid for r in n["resp"]):
                return {"method": "GET", "path": _path(n["url"])}
    detail = _path(e["url"])
    for n in nets:
        p = _path(n.get("url", ""))
        if n.get("method", "").upper() == "GET" and n.get("origin") == e["origin"] and p != detail \
                and detail.startswith(p.rstrip("/") + "/"):
            return {"method": "GET", "path": p}
    if rid is not None and detail.rstrip("/").endswith("/" + str(rid)):
        return {"method": "GET", "path": detail.rstrip("/")[: -len(str(rid)) - 1] or "/"}
    return {"method": "GET", "path": detail}
