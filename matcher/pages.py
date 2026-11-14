"""Page -> form candidates (docs/contracts.md V11). Pure: no network, no LLM; reads only the recording's own
page snapshots (snapshots/<n>.html, saved by the recorder on copy) when a base directory is given.

A page -> form recording has no mutating JSON request; its submit is a button click that carries the form's
questions (`form.questions`), and values are typed or pasted into controls that carry `el.question`.
Each destination value is explained, in order, by:
  1. a copy on the source page -> `css` (the copied element; `regex` refines a part of its text),
  2. the page URL -> `record` `record.canonical_url`; the recording's date -> `record` `record.captured_date`,
  3. the page itself (typed by hand) -> `jsonld`, else `css` of an element whose text is exactly the value,
  4. a typed value found nowhere -> `const`; a pasted one found nowhere -> unexplained.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runner.html import canonical_url, css_all, jsonld_objects, normalize_ws, site_of
from selectolax.lexbor import LexborHTMLParser

CONFIRM_WORDS = re.compile(r"recorded|thank|received|submitted|success|sent|saved", re.I)
UNSTABLE_CLASS = re.compile(r"^(css|sc|jsx|emotion|svelte)-|[0-9a-f]{6,}|\d{3,}|^_|"
                            r"^(is-|has-)?(active|hover|focus|focused|selected|open|visible|hidden|highlighted)$", re.I)
SAFE_IDENT = re.compile(r"^[A-Za-z_-][\w-]*$")
GENERATED_ID = re.compile(r"\d{3,}|^[a-z]{0,2}\d+$|^(ember|react|radix|:r)|[0-9a-f]{8,}", re.I)
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FIELD_EVENTS = ("input", "change", "paste")


def _n(v: Any) -> str:
    return normalize_ws(str(v)).casefold()


def _esc(s: str) -> str:
    # Spaces and '-' need no escape outside a character class; leaving them bare keeps regexes readable.
    return re.escape(s).replace("\\ ", " ").replace("\\-", "-")


def submit_click(recording: dict) -> dict | None:
    """The last button click that carried form questions (before the last confirmation, if any)."""
    events = recording.get("events", [])
    confirmations = [e for e in events if e.get("type") == "confirmation"]
    clicks = [e for e in events if e.get("type") == "click" and isinstance(e.get("form"), dict)
              and e["form"].get("questions")]
    if confirmations:
        before = [c for c in clicks if c.get("t", 0) <= confirmations[-1].get("t", 0)]
        clicks = before or clicks
    return clicks[-1] if clicks else None


def is_page_form(recording: dict) -> bool:
    return submit_click(recording) is not None


# ---------------------------------------------------------------- the sink
def _confirmation_text(recording: dict, after_t: int) -> str | None:
    for e in recording.get("events", []):
        if e.get("type") == "confirmation" and e.get("t", 0) >= after_t:
            for line in e.get("lines") or []:
                if CONFIRM_WORDS.search(line):
                    return normalize_ws(line).rstrip(".!")
    return None


def _sink(recording: dict, click: dict) -> dict:
    questions = click["form"]["questions"]
    url = (click.get("url") or "").split("#")[0]
    return {"kind": "form", "url": url, "labels": [q["label"] for q in questions],
            "submit": (click.get("el") or {}).get("accessible_name") or "Submit",
            "confirmation_text": _confirmation_text(recording, click.get("t", 0)),
            "title": click["form"].get("title"), "questions": questions}


def _values(recording: dict, click: dict, labels: list[str]) -> list[tuple[str, Any, str]]:
    """Final (label, value, how) per form question; how = the last event type that set it."""
    by_label = {_n(lab): lab for lab in labels}
    vals: dict[str, tuple[Any, str]] = {}
    for e in recording.get("events", []):
        if e.get("type") not in FIELD_EVENTS or e.get("t", 0) > click.get("t", 0) or not e.get("el"):
            continue
        el = e["el"]
        key = next((by_label[_n(k)] for k in (el.get("question"), el.get("label"), el.get("name"))
                    if k and _n(k) in by_label), None)
        if key is not None:
            vals[key] = (e.get("value"), e["type"])
    return [(lab, *vals[lab]) for lab in labels if lab in vals and vals[lab][0] not in (None, "")]


# ---------------------------------------------------------------- the source page
def _source(recording: dict, click: dict, base_dir: Path | None) -> tuple[dict | None, str | None, list[dict]]:
    copies = [e for e in recording.get("events", []) if e.get("type") == "copy" and e.get("text")
              and e.get("t", 0) <= click.get("t", 0) and _origin(e.get("url")) != _origin(click.get("url"))]
    with_snap = [c for c in copies if c.get("snapshot")]
    if not copies:
        return None, None, []
    last = (with_snap or copies)[-1]
    url = last.get("url") or ""
    html = None
    if last.get("snapshot") and base_dir is not None:
        try:
            html = (Path(base_dir) / last["snapshot"]).read_text(encoding="utf-8")
        except OSError:
            html = None
    title = next((e.get("title") for e in reversed(recording.get("events", []))
                  if e.get("type") == "framenavigated" and e.get("url") == url and e.get("title")), None)
    src = {"kind": "page", "site": site_of(url), "url": url, "canonical_url": canonical_url(url, html or ""),
           "snapshot": last.get("snapshot"), "title": title}
    return src, html, [c for c in copies if c.get("url") == url]


def _origin(url: str | None) -> str:
    m = re.match(r"^[a-z]+://[^/]+", url or "")
    return m.group(0) if m else ""


def _stable_classes(node) -> list[str]:
    cls = (node.attributes.get("class") or "").split()
    return [c for c in cls if SAFE_IDENT.match(c) and not UNSTABLE_CLASS.search(c)]


def _selector_candidates(node) -> list[str]:
    """Readable selectors for `node`, nearest-context first: `.ancestor-class tag`, `#id tag`, `tag.class`."""
    tag = node.tag
    out = []
    cur, depth = node.parent, 0
    while cur is not None and cur.tag not in ("body", "html", "-undef") and depth < 4:
        ident = cur.attributes.get("id")
        if ident and SAFE_IDENT.match(ident) and not GENERATED_ID.search(ident):
            out.append(f"#{ident} {tag}")
        out += [f".{c} {tag}" for c in _stable_classes(cur)]
        cur, depth = cur.parent, depth + 1
    out += [f"{tag}.{c}" for c in _stable_classes(node)]
    return out


def best_selector(html: str, path: str | None, element_text: str | None = None) -> str | None:
    """A readable selector equivalent to the recorded element `path` on this page: every match has the same
    text as the element. Falls back to `path` itself."""
    try:
        tree = LexborHTMLParser(html)
        nodes = tree.css(path) if path else []
    except Exception:  # noqa: BLE001 - selectolax rejects a selector
        nodes = []
    if len(nodes) != 1:
        if element_text is None:
            return path
        nodes = [n for n in tree.css("body *") if normalize_ws(n.text(deep=True, separator=" ")) == element_text]
        nodes = [n for n in nodes if not any(normalize_ws(c.text(deep=True, separator=" ")) == element_text
                                             for c in n.iter())][:1]
        if not nodes:
            return path
    node = nodes[0]
    text = normalize_ws(node.text(deep=True, separator=" "))
    for sel in _selector_candidates(node):
        got = css_all(html, sel)
        if got and all(g == text for g in got):
            return sel
    return path


def refine_regex(whole: str, value: str) -> str | None:
    """A regex whose group 1 pulls `value` out of `whole` (an element's normalised text), short context only."""
    i = whole.casefold().find(value.casefold())
    if i < 0:
        return None
    prefix, suffix = whole[:i], whole[i + len(value):]

    def tail(s: str) -> str:   # a long prefix keeps only its last word: "... at (.+)$"
        if len(s) <= 20:
            return "^" + _esc(s)
        return "^.*" + _esc(re.search(r"\s?\S*\s*$", s).group(0))

    def head(s: str) -> str:   # a long suffix keeps only its first word: "^(.+?) - "
        if len(s) <= 20:
            return _esc(s) + "$"
        return _esc(re.match(r"\s*\S*\s?", s).group(0))

    rx = (tail(prefix) if prefix else "^") + ("(.+?)" if suffix else "(.+)") + (head(suffix) if suffix else "$")
    try:
        m = re.search(rx, whole)
    except re.error:
        return None
    return rx if m and _n(m.group(1)) == _n(value) else None


def _jsonld_paths(html: str, value: Any) -> list[dict]:
    out: list[dict] = []

    def walk(obj: Any, path: str, type_: str) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if not k.startswith("@"):
                    walk(v, f"{path}.{k}" if path else k, type_)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                walk(v, f"{path}[{i}]", type_)
        elif isinstance(obj, str) and path and _n(obj) == _n(value):
            alt = {"jsonld": path}
            if type_ != "JobPosting":
                alt["jsonld_type"] = type_
            if alt not in out:
                out.append(alt)

    seen_types: set[str] = set()
    for obj in jsonld_objects(html):
        t = obj.get("@type")
        t = t[0] if isinstance(t, list) and t else t
        if isinstance(t, str) and t not in seen_types:  # jsonld_get reads the first object of a type
            seen_types.add(t)
            walk(obj, "", t)
    out.sort(key=lambda a: "jsonld_type" in a)  # JobPosting first
    return out


def _meta_alternatives(html: str, value: Any) -> list[dict]:
    out = []
    try:
        metas = LexborHTMLParser(html).css("meta")
    except Exception:  # noqa: BLE001
        return out
    for m in metas:
        key = next(((a, m.attributes.get(a)) for a in ("property", "name") if m.attributes.get(a)), None)
        content = m.attributes.get("content")
        if key and content and SAFE_IDENT.match(key[1].replace(":", "_")) and _n(content) == _n(value):
            out.append({"css": f'meta[{key[0]}="{key[1]}"]', "attr": "content"})
    return out


def _alternatives(html: str | None, value: Any, primary: dict) -> list[dict]:
    if not html:
        return []
    alts = _jsonld_paths(html, value) + _meta_alternatives(html, value)
    return [a for a in alts if not all(primary.get(k) == v for k, v in a.items())]


# ---------------------------------------------------------------- explanations
def _from_copy(copies: list[dict], value: Any, html: str | None) -> dict | None:
    hit = next((c for c in reversed(copies) if _n(c["text"]) == _n(value)), None) or \
        next((c for c in reversed(copies) if _n(value) in _n(c["text"])), None)
    if hit is None:
        return None
    el = hit.get("el") or {}
    path = el.get("path") or el.get("css")
    m = {"kind": "css", "css": path, "attr": None, "regex": None, "copied": True}
    if html is None:
        return m
    whole = css_all(html, path)[0] if path and css_all(html, path) else None
    if whole is None:
        return m
    m["css"] = best_selector(html, path, whole) or path
    if _n(whole) != _n(value):
        m["regex"] = refine_regex(whole, normalize_ws(str(value)))
    return m


def _dates(recording: dict, click: dict) -> set[str]:
    out = set()
    for t in (click.get("t"), recording.get("started_at"), recording.get("stopped_at")):
        if isinstance(t, (int, float)):
            out.add(datetime.fromtimestamp(t / 1000).date().isoformat())
            out.add(datetime.fromtimestamp(t / 1000, tz=timezone.utc).date().isoformat())
    return out


def _from_page(html: str | None, value: Any) -> dict | None:
    """A value typed by hand that the page shows anyway: JSON-LD first, else an element with exactly that text."""
    if not html:
        return None
    paths = _jsonld_paths(html, value)
    if paths:
        return {"kind": "jsonld", **paths[0], "attr": None, "regex": None}
    sel = best_selector(html, None, normalize_ws(str(value)))
    if sel:
        return {"kind": "css", "css": sel, "attr": None, "regex": None}
    return None


def match_page_form(recording: dict, base_dir: Path | None = None) -> dict:
    click = submit_click(recording)
    sink = _sink(recording, click)
    source, html, copies = _source(recording, click, base_dir)
    page_urls = {_n(u) for u in ((source or {}).get("url"), (source or {}).get("canonical_url")) if u}
    dates = _dates(recording, click)

    mappings, unexplained = [], []
    for label, value, how in _values(recording, click, sink["labels"]):
        base = {"dest_field": label, "value": value}
        text = normalize_ws(str(value))
        m = _from_copy(copies, value, html)
        if m is None and _n(text).rstrip("/") in {u.rstrip("/") for u in page_urls}:
            m = {"kind": "record", "from": "record.canonical_url"}
        elif m is None and ISO_DATE.match(text) and text in dates:
            m = {"kind": "record", "from": "record.captured_date"}
        if m is None:
            m = _from_page(html, value)
        if m is None:
            if how == "paste":
                unexplained.append(base)
            else:
                mappings.append({**base, "kind": "const"})
            continue
        if m["kind"] in ("css", "jsonld"):
            m["alternatives"] = _alternatives(html, value, m)
        mappings.append({**base, **m})

    return {"recording_id": recording.get("id"), "intent": recording.get("intent"), "source": source,
            "sink": sink, "mappings": mappings, "unexplained": unexplained}
