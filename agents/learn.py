"""Deterministic helpers for the LEARN lane (contracts V10). No langchain, no network, no AI.

These are the checks behind the learn tools in agents/deep_tools.py, and the scripted agent calls the
same functions, so the guardrails are code whichever agent runs:

- `page_outline`      the compact view of a page the learner gets instead of its HTML
- `run_page_parser`   a candidate parser on one page record, via runner.lib's own extract helpers
- `evidence`          every extracted value occurs on the page and passes the spec's rules
- `corroborated`      the value also occurs in the page's title / og: / twitter: meta / JSON-LD
- `ambiguity`         a css selector (or JSON-LD path) that reads different values on the same page
- `additive_parser_patch`, `d2_decision`   the D2 auto-use rule
"""

from __future__ import annotations

import difflib
import json
import re
from datetime import datetime, timezone
from typing import Any

import jsonpatch
import yaml
from selectolax.lexbor import LexborHTMLParser

from runner.html import (canonical_url, css_all, css_first, jsonld_objects, normalize_ws, site_of,
                         visible_text)
from runner.clean import clean_cast
from runner.lib import (Quarantine, cast, extract_between, extract_css, extract_json_path, extract_jsonld,
                        extract_product, extract_regex, refine_regex, rule_problem_date, rule_problem_number,
                        rule_problem_url)
from runner.rows import Row, check_cross, explain_skip, extract_rows, select_rows

PAGE_METHODS = ("css", "jsonld")

# Page specs carry whole pages in their golden examples (a Lever page is ~700 KB): use libyaml when present.
_YLoader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_YDumper = getattr(yaml, "CSafeDumper", yaml.SafeDumper)


def yload(text: str) -> Any:
    return yaml.load(text, Loader=_YLoader)  # noqa: S506 - a safe loader


def ydump(obj: Any, **kw: Any) -> str:
    return yaml.dump(obj, Dumper=_YDumper, allow_unicode=kw.pop("allow_unicode", True), **kw)
# Corroboration sources longer than this (descriptions) only count on an exact match, otherwise any
# sentence of the posting body would "corroborate" itself.
CORROBORATION_MAX_SOURCE = 200
_STABLE_CLASS = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_HASHY = re.compile(r"\d{3,}|_[a-z0-9]{5}_|^css-|^sc-|^jsx-")
_SKIP_META = re.compile(r"^(og:image|og:url|og:locale|og:type|twitter:card|twitter:image|og:image:.*)$")
_SKIP_JSONLD_KEYS = {"description", "url", "image", "logo", "sameAs", "potentialAction", "breadcrumb", "isPartOf",
                     "inLanguage", "target"}


# ---- page records ----------------------------------------------------------------------------

def page_text(record: dict, text_field: str = "text") -> str:
    text = record.get(text_field)
    return text if isinstance(text, str) and text else visible_text(record.get("html") or "")


def make_page_record(url: str, html: str, *, seq: int = 1, title: str | None = None,
                     captured_at: str | None = None) -> dict:
    """A V1 page record, built the way the page inbox builds one (tests and TEACH goldens use it)."""
    at = captured_at or datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")
    return {"id": f"pg-{seq:06d}", "seq": seq, "url": url, "canonical_url": canonical_url(url, html),
            "site": site_of(url), "title": title if title is not None else (css_first(html, "title") or ""),
            "html": html, "text": visible_text(html), "captured_at": at, "captured_date": at[:10]}


# ---- outline -----------------------------------------------------------------------------------

def _stable_classes(node) -> list[str]:
    return [c for c in (node.attributes.get("class") or "").split()
            if _STABLE_CLASS.match(c) and not _HASHY.search(c) and not c.startswith("_")]


def css_path(html: str, node) -> tuple[str, int]:
    """A short selector that reads exactly this node's value on this page. Returns (selector, number
    of distinct values it reads): 1 means unambiguous."""
    value = normalize_ws(node.text(deep=True, separator=" "))
    tag = node.tag
    cands = []
    node_id = node.attributes.get("id")
    if node_id and _STABLE_CLASS.match(node_id) and not _HASHY.search(node_id):
        cands.append(f"#{node_id}")
    cands += [f"{tag}.{c}" for c in _stable_classes(node)]
    parent = node.parent
    if parent is not None and parent.tag not in ("body", "html", "-undef"):
        cands += [f".{c} {tag}" for c in _stable_classes(parent)]
        if parent.attributes.get("id") and not _HASHY.search(parent.attributes["id"]):
            cands.append(f"#{parent.attributes['id']} {tag}")
    cands.append(tag)
    for sel in cands:
        vals = [v for v in css_all(html, sel) if v]
        if vals and vals[0] == value and len(set(vals)) == 1:
            return sel, 1
    return tag, len(set(v for v in css_all(html, tag) if v))


def _jsonld_leaves(obj: Any, prefix: str = "", depth: int = 0) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if depth > 3:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.startswith("@") or k in _SKIP_JSONLD_KEYS:
                continue
            out += _jsonld_leaves(v, f"{prefix}.{k}" if prefix else k, depth + 1)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:3]):
            out += _jsonld_leaves(v, f"{prefix}[{i}]", depth + 1)
    elif isinstance(obj, str) and normalize_ws(obj):
        out.append((prefix, normalize_ws(obj)))
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out.append((prefix, str(obj)))  # prices, ratings, counts (V15: not every integration reads text)
    return out


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _types_of(obj: dict) -> list[str]:
    t = obj.get("@type")
    return [t] if isinstance(t, str) else [x for x in t if isinstance(x, str)] if isinstance(t, list) else []


def jsonld_types(html: str) -> list[str]:
    """Every JSON-LD object @type on the page, in page order, each once (V15: get_page lists them all)."""
    out: list[str] = []
    for obj in jsonld_objects(html or ""):
        for t in _types_of(obj):
            if t not in out:
                out.append(t)
    return out


def infer_jsonld_type(html: str, path: str) -> str | None:
    """The @type a `jsonld` reader without `jsonld_type` reads from on this page: JobPosting when the path
    resolves there (the runtime default, so nothing changes for job pages), else the first type on the page
    whose first object has a value at `path` (exactly what jsonld_get will read at runtime). None if no type."""
    from runner.html import jsonld_get
    types = jsonld_types(html)
    for t in (["JobPosting"] if "JobPosting" in types else []) + [t for t in types if t != "JobPosting"]:
        v = jsonld_get(html or "", path, t)
        if v not in (None, "") and not isinstance(v, (dict, list)):
            return t
    return None


def with_jsonld_types(parser: dict, html: str) -> dict:
    """A copy of the parser whose `jsonld` readers all carry an explicit `jsonld_type` (V15), filled from the
    page when missing. A reader whose path resolves nowhere keeps the runtime default (it fails its test)."""
    p = json.loads(json.dumps(parser))
    for f in (p.get("fields") or {}).values():
        if isinstance(f, dict) and f.get("jsonld") is not None and not f.get("jsonld_type"):
            f["jsonld_type"] = infer_jsonld_type(html, str(f["jsonld"])) or "JobPosting"
    return p


def page_outline(record: dict, max_chars: int = 4000, prefer_types: list[str] | None = None) -> str:
    """Title, h1-h3, og/twitter meta and JSON-LD leaves, each with the extractor that reads it, then
    the start of the visible text. Never the whole HTML (the learner's budget is ~8 model calls).
    `prefer_types` (the JSON-LD types the integration already reads) are listed first."""
    html = record.get("html") or ""
    lines = [f"page {record.get('id')} · site {record.get('site')} · url {record.get('canonical_url') or record.get('url')}",
             f"document.title: {json.dumps(_clip(normalize_ws(record.get('title') or ''), 200))}"]
    head_title = next((v for v in css_all(html, "head title") if v), None)
    if head_title:
        lines.append(f"  {{css: \"head title\"}} → {json.dumps(_clip(head_title, 200))}")
    try:
        tree = LexborHTMLParser(html)
        heads = tree.css("h1, h2, h3")
        metas = tree.css("meta")
    except Exception:  # noqa: BLE001 - never raise on a bad page
        heads, metas = [], []
    lines.append("headings ({css: …} → text; '(N values)' = the selector reads N different values):")
    shown: dict[str, int] = {}
    for node in heads[:30]:
        text = normalize_ws(node.text(deep=True, separator=" "))
        if not text:
            continue
        sel, n = css_path(html, node)
        shown[sel] = shown.get(sel, 0) + 1
        if n > 1 and shown[sel] > 2:
            continue  # an ambiguous selector: two examples are enough
        lines.append(f"  {node.tag} {{css: {json.dumps(sel)}}}{f' ({n} values)' if n > 1 else ''} → {json.dumps(_clip(text, 120))}")
    lines.append("meta ({css: …, attr: content} → value):")
    for m in metas:
        key = m.attributes.get("property") or m.attributes.get("name") or ""
        content = normalize_ws(m.attributes.get("content") or "")
        if not content or not key.startswith(("og:", "twitter:")) or _SKIP_META.match(key):
            continue
        attr_name = "property" if m.attributes.get("property") else "name"
        selector = 'meta[%s="%s"]' % (attr_name, key)
        lines.append(f"  {{css: {json.dumps(selector)}, attr: content}} → {json.dumps(_clip(content, 100))}")
    types = jsonld_types(html)
    lines.append(f"json-ld types on this page: {', '.join(types) if types else 'none'}")
    lines.append("json-ld ({jsonld: path, jsonld_type: T} → value):")
    n_leaves = 0
    prefer = [t for t in (prefer_types or []) if t]
    objs = [o for o in jsonld_objects(html) if _types_of(o)]
    objs.sort(key=lambda o: next((i for i, t in enumerate(prefer) if t in _types_of(o)), len(prefer)))
    for obj in objs:
        typ = next((t for t in prefer if t in _types_of(obj)), _types_of(obj)[0])
        for path, value in _jsonld_leaves(obj):
            if n_leaves >= 30:
                break
            if value.startswith(("http://", "https://")):
                continue
            n_leaves += 1
            lines.append(f"  {{jsonld: {json.dumps(path)}, jsonld_type: {typ}}} → {json.dumps(_clip(value, 80))}")
    out = "\n".join(lines)
    text_lines = page_text(record).splitlines()
    if text_lines and len(out) < max_chars - 200:
        out += "\nvisible text (start):"
        for ln in text_lines:
            add = "\n  " + _clip(ln, 160)
            if len(out) + len(add) > max_chars - 40:
                out += "\n  …"
                break
            out += add
    return out[:max_chars]


# ---- what the integration already knows (V15: prompts and tools name the integration's own fields) -----

def _parsers(spec: dict | None) -> list[dict]:
    return list(((spec or {}).get("extract") or {}).get("parsers") or [])


def integration_fields(spec: dict | None) -> list[str]:
    """The fields every parser extracts, in the first parser's order (the spec order)."""
    parsers = _parsers(spec)
    if not parsers:
        return []
    common = set.intersection(*(set(p.get("fields") or {}) for p in parsers))
    return [f for f in parsers[0].get("fields") or {} if f in common]


def integration_jsonld_types(spec: dict | None) -> list[str]:
    """The JSON-LD @types the integration's readers use (`JobPosting` for a jsonld reader without a type)."""
    out: list[str] = []
    for p in _parsers(spec):
        for f in (p.get("fields") or {}).values():
            if isinstance(f, dict) and f.get("jsonld") is not None:
                t = f.get("jsonld_type") or "JobPosting"
                if t not in out:
                    out.append(t)
    return out


def known_readers(spec: dict | None, per_field: int = 3) -> str:
    """`price: jsonld offers.price (Product) | css .price; name: ...`: how the existing parsers read each field."""
    parts = []
    for name in integration_fields(spec):
        labels: list[str] = []
        for p in _parsers(spec):
            f = (p.get("fields") or {}).get(name)
            if isinstance(f, dict):
                lab = extractor_label(f)
                if lab not in labels:
                    labels.append(lab)
        if labels:
            parts.append(f"{name}: {' | '.join(labels[:per_field])}")
    return "; ".join(parts)


def rule_label(rule: dict) -> str:
    """`title: required`, `price: number (min 0, max 10000)`, `link: url`."""
    kind = rule.get("rule")
    extra = ""
    if kind == "number":
        bounds = [f"{k} {rule[k]:g}" if isinstance(rule.get(k), (int, float)) else f"{k} {rule[k]}"
                  for k in ("min", "max") if rule.get(k) is not None]
        extra = f" ({', '.join(bounds)})" if bounds else ""
    elif kind == "regex":
        extra = f" /{rule.get('pattern')}/"
    elif kind == "one_of":
        extra = f" {rule.get('values')}"
    elif kind == "date":
        extra = " (YYYY-MM-DD)"
    elif kind == "url":
        extra = " (absolute http/https)"
    return f"{rule.get('field')}: {kind}{extra}"


# ---- running a candidate parser ---------------------------------------------------------------

def extractor_label(f: dict) -> str:
    """Short human label for a field extractor, e.g. `css .posting-headline h2` or `jsonld title`."""
    if f.get("css") is not None:
        s = f"css {f['css']}" + (f" @{f['attr']}" if f.get("attr") else "")
    elif f.get("jsonld") is not None:
        s = f"jsonld {f['jsonld']}" + (f" ({f['jsonld_type']})" if f.get("jsonld_type") else "")
    elif f.get("json_path") is not None:
        return f"json_path {f['json_path']}"
    elif f.get("between") is not None:
        return f"between {f['between']}"
    elif f.get("regex") is not None:
        return f"regex {f['regex']}"
    else:
        return "?"
    return s + (f" refine /{f['regex']}/" if f.get("regex") else "")


def extract_field(f: dict, record: dict, where: str, html_field: str = "html", text_field: str = "text") -> Any:
    """One field, exactly as the generated parser does it (runner.lib / runner.clean helpers only): css, jsonld,
    json_path, regex or between, then the refine regex (a regex next to css, jsonld or json_path), then `clean`
    and `cast` (contracts V18)."""
    html = record.get(html_field) or ""
    if not isinstance(f, dict):
        raise Quarantine("PARSE_NO_MATCH", f"{where}: extractor must be a mapping")
    to = f.get("cast", "str")
    if f.get("combine") == "product" and f.get("regex") is not None:
        return extract_product(page_text(record, text_field), re.compile(f["regex"]), to, where)
    refine = False
    if f.get("css") is not None:
        value: Any = extract_css(html, str(f["css"]), f.get("attr"), where)
        refine = True
    elif f.get("jsonld") is not None:
        value = extract_jsonld(html, str(f["jsonld"]), f.get("jsonld_type") or "JobPosting", where)
        refine = True
    elif f.get("attr") is not None or f.get("jsonld_type") is not None:
        raise Quarantine("PARSE_NO_MATCH", f"{where}: attr needs css and jsonld_type needs jsonld")
    elif f.get("json_path") is not None:
        value = extract_json_path(record, f["json_path"], where)
        refine = True
    elif f.get("regex") is not None:
        value = extract_regex(page_text(record, text_field), re.compile(f["regex"]), where)
    elif f.get("between") is not None:
        value = extract_between(page_text(record, text_field), f["between"][0], f["between"][1], where)
    else:
        raise Quarantine("PARSE_NO_MATCH", f"{where}: no extract method (use css, jsonld, regex, json_path or between)")
    if refine and f.get("regex") is not None:
        value = refine_regex(str(value), re.compile(f["regex"]), where)
    return clean_cast(value, f.get("clean"), to, where)


def run_page_parser(parser: dict, record: dict, html_field: str = "html", text_field: str = "text") -> dict:
    """The candidate's fields on this page, or {"error": "...", "fields": <the ones that worked>}."""
    if not isinstance(parser, dict) or not isinstance(parser.get("fields"), dict) or not parser["fields"]:
        return {"error": "parser needs a non-empty `fields` mapping"}
    pid = parser.get("id") or "parser"
    fields, errors = {}, []
    for name, f in parser["fields"].items():
        try:
            fields[name] = extract_field(f, record, f"{pid}.{name}", html_field, text_field)
        except Quarantine as q:
            errors.append(q.detail or q.code)
        except (re.error, TypeError, ValueError, KeyError, IndexError) as e:
            errors.append(f"{pid}.{name}: {type(e).__name__}: {e}")
    return {"error": "; ".join(errors), "fields": fields} if errors else fields


# ---- checks ----------------------------------------------------------------------------------

def _jsonld_strings(html: str) -> list[str]:
    out: list[str] = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str):
            s = normalize_ws(o)
            if s:
                out.append(s)
        elif isinstance(o, (int, float)) and not isinstance(o, bool):
            out.append(str(o))
    for obj in jsonld_objects(html):
        walk(obj)
    return out


def _attr_values(html: str) -> list[str]:
    try:
        nodes = LexborHTMLParser(html).css("*")
    except Exception:  # noqa: BLE001
        return []
    return [normalize_ws(v) for n in nodes for v in n.attributes.values() if v and normalize_ws(v)]


def _check_rules(name: str, value: Any, validate: list[dict]) -> str | None:
    for rule in validate or []:
        if rule.get("field") != name:
            continue
        kind = rule.get("rule")
        if kind == "required" and value in (None, ""):
            return "required"
        if kind == "regex" and not re.search(rule.get("pattern") or "", str(value)):
            return f"does not match {rule.get('pattern')!r}"
        if kind == "one_of" and value not in (rule.get("values") or []):
            return f"not one of {rule.get('values')}"
        if kind == "positive_int":
            try:
                if int(value) <= 0:
                    return "not a positive integer"
            except (TypeError, ValueError):
                return "not a positive integer"
        # contracts V15: the same checks the generated validate() runs (runner.lib)
        problem = None
        if kind == "number":
            problem = rule_problem_number(value, rule.get("min"), rule.get("max"))
        elif kind == "date":
            problem = rule_problem_date(value)
        elif kind == "url":
            problem = rule_problem_url(value)
        if problem:
            return problem
    return None


def literal_capture(pattern: str) -> str | None:
    """The fixed text a regex's group 1 captures when that group has no wildcard, class or repeat in it
    (`About Us (Visa) is` -> "Visa"): such a reader writes the company in instead of reading it from the page.
    None when group 1 reads variable text (or the pattern does not parse)."""
    try:
        import re._parser as sre  # Python 3.11+
        tree = sre.parse(pattern)
    except Exception:  # noqa: BLE001
        return None

    def group1(items):
        for op, av in items:
            if op is sre.SUBPATTERN and av[0] == 1:
                return av[3]
            if op is sre.SUBPATTERN:
                found = group1(av[3])
                if found is not None:
                    return found
        return None

    body = group1(list(tree))
    if not body or any(op is not sre.LITERAL for op, _ in body):
        return None
    return "".join(chr(av) for _, av in body)


_NUM_TOKEN = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_EU_NUM_TOKEN = re.compile(r"-?\d[\d.]*,\d+")


def is_document(record: dict) -> bool:
    """A V17 document record (`doc-…`), as opposed to a V1 page."""
    return str(record.get("id") or "").startswith("doc-") or "tables" in record


def _numbers_in(text: str) -> list[float]:
    out = []
    for tok in _NUM_TOKEN.findall(text):
        try:
            out.append(float(tok.replace(",", "")))
        except ValueError:
            pass
    for tok in _EU_NUM_TOKEN.findall(text):
        try:
            out.append(float(tok.replace(".", "").replace(",", ".")))
        except ValueError:
            pass
    return out


def value_forms(value: Any, spec: dict | None = None) -> list[str]:
    """The strings a value read from a document may have been written as. A cleaned number (`$1,299.00` ->
    1299.0) or date (`03 Nov 2026` -> `2026-11-03`) is not literally in the text, so the forms it could have taken
    are tried: the value itself, a number with 0 or 2 decimals and thousands separators, and an ISO date in each of
    the formats the field's `clean: {date: [...]}` declares."""
    forms = [normalize_ws(str(value))] if value is not None else []
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        forms += [f"{value:,.2f}", f"{value:.2f}", f"{value:,}", f"{value}"]
        if float(value).is_integer():
            forms += [f"{int(value):,}", str(int(value))]
    clean = (spec or {}).get("clean")
    if isinstance(value, str) and isinstance(clean, dict) and isinstance(clean.get("date"), list):
        try:
            day = datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            day = None
        for fmt in clean["date"] if day else []:
            text = day.strftime(fmt)
            forms += [text, re.sub(r"(?<!\d)0(\d)", r"\1", text)]
    return list(dict.fromkeys(f for f in forms if f))


def _document_haystacks(record: dict) -> dict[str, str]:
    """`table`: every table cell and header; `meta`: every string in the record's meta (sender, subject, …)."""
    cells = []
    for t in record.get("tables") or []:
        cells += [normalize_ws(str(c)) for c in (t.get("header") or [])]
        for row in t.get("rows") or []:
            cells += [normalize_ws(str(c)) for c in row]
    meta = [normalize_ws(str(v)) for v in (record.get("meta") or {}).values() if isinstance(v, (str, int, float))]
    return {"table": "\n".join(c for c in cells if c), "meta": "\n".join(m for m in meta if m)}


def evidence(record: dict, fields: dict, validate: list[dict], parser: dict | None = None,
             html_field: str = "html", text_field: str = "text") -> list[dict]:
    """V10 evidence items: {name, value, ok, where: text|table|meta|jsonld|html|None, extractor}. A value is
    evidenced if it occurs (whitespace-normalised) in the visible text, a JSON-LD string, an attribute
    value or the <title>, and it passes the spec's validation rules. record.* values never reach here.
    V17/V18: on a document record a value may also occur in a table cell or a `meta` string, and a cleaned number
    or date counts when one of the forms it could have been written as (`value_forms`) occurs."""
    html = record.get(html_field) or ""
    text = normalize_ws(page_text(record, text_field))
    doc = is_document(record)
    stacks = _document_haystacks(record) if doc else {}
    lazy: dict[str, list[str]] = {}
    items = []
    for name, value in fields.items():
        spec = ((parser or {}).get("fields") or {}).get(name) or {}
        forms = value_forms(value, spec) if doc else ([normalize_ws(str(value))] if value is not None else [])
        where = None
        if forms:
            if any(f in text for f in forms):
                where = "text"
            elif doc and any(f in stacks["table"] for f in forms):
                where = "table"
            elif doc and any(f in stacks["meta"] for f in forms):
                where = "meta"
            elif doc and isinstance(value, (int, float)) and not isinstance(value, bool) and any(
                    abs(n - float(value)) < 1e-9 for n in _numbers_in(text + "\n" + stacks["table"])):
                where = "text"
            else:
                if "jsonld" not in lazy:
                    lazy["jsonld"] = _jsonld_strings(html)
                if any(f in s for f in forms for s in lazy["jsonld"]):
                    where = "jsonld"
                else:
                    if "html" not in lazy:
                        lazy["html"] = _attr_values(html) + [t for t in css_all(html, "head title") if t]
                    if any(f in s for f in forms for s in lazy["html"]):
                        where = "html"
        rule_problem = _check_rules(name, value, validate)
        item = {"name": name, "value": value, "ok": where is not None and rule_problem is None, "where": where,
                "extractor": extractor_label(spec)}
        if where is None:
            item["detail"] = "value does not occur on the page" if not doc else "value does not occur in the document"
        elif rule_problem:
            item["detail"] = f"fails validation: {rule_problem}"
        items.append(item)
    return items


def _occurs(value: str, source: str) -> bool:
    return re.search(r"(?<![0-9a-z])" + re.escape(value) + r"(?![0-9a-z])", source) is not None


def corroboration_sources(record: dict, html_field: str = "html") -> list[tuple[str, str]]:
    html = record.get(html_field) or ""
    out = [("document.title", normalize_ws(record.get("title") or ""))]
    out += [("<title>", t) for t in css_all(html, "head title") if t]
    try:
        metas = LexborHTMLParser(html).css("meta")
    except Exception:  # noqa: BLE001
        metas = []
    for m in metas:
        key = m.attributes.get("property") or m.attributes.get("name") or ""
        if key.startswith(("og:", "twitter:")) and m.attributes.get("content"):
            out.append((key, normalize_ws(m.attributes["content"])))
    out += [("json-ld", s) for s in _jsonld_strings(html)]
    return [(k, v) for k, v in out if v]


def corroborated(record: dict, value: Any, html_field: str = "html") -> bool:
    """V10: the value also occurs (normalised, case-insensitive, as whole words) in document.title, the
    <title>, an og:*/twitter:* meta value or a JSON-LD string. Long sources (descriptions) only count on
    an exact match. Page chrome (cookie banners, "Apply") is not corroborated."""
    v = normalize_ws(str(value or "")).lower()
    if len(v) < 2:
        return False
    for key, src in corroboration_sources(record, html_field):
        s = src.lower()
        if key not in ("document.title", "<title>") and len(s) > CORROBORATION_MAX_SOURCE:
            if s == v:
                return True
            continue
        if _occurs(v, s):
            return True
    return False


def ambiguity(record: dict, parser: dict, html_field: str = "html") -> dict[str, list[str]]:
    """Fields whose selector reads more than one distinct value on this page: {field: [values…]}.
    A css field with a refine regex counts only the matches the refine accepts."""
    html = record.get(html_field) or ""
    out: dict[str, list[str]] = {}
    for name, f in (parser.get("fields") or {}).items():
        if not isinstance(f, dict):
            continue
        values: list[str] = []
        if f.get("css") is not None:
            values = css_all(html, str(f["css"]), f.get("attr"))
        elif f.get("jsonld") is not None:
            from runner.html import _has_type, _PATH_TOKEN  # same path walk as jsonld_get
            typ = f.get("jsonld_type") or "JobPosting"
            for obj in jsonld_objects(html):
                if not _has_type(obj, typ):
                    continue
                cur: Any = obj
                for key, idx in _PATH_TOKEN.findall(str(f["jsonld"])):
                    try:
                        cur = cur[int(idx)] if idx else cur[key]
                    except (KeyError, IndexError, TypeError):
                        cur = None
                        break
                if isinstance(cur, (str, int, float)):
                    values.append(normalize_ws(str(cur)))
        elif is_document(record) and f.get("regex") is not None and f.get("json_path") is None \
                and f.get("between") is None:
            # V18: a document-level regex that matches twice with different values reads two things
            try:
                pat = re.compile(f["regex"])
            except re.error:
                continue
            found = [normalize_ws(m.group(1)) for m in pat.finditer(page_text(record)) if m.groups() and m.group(1)]
            distinct = list(dict.fromkeys(v for v in found if v))
            if len(distinct) > 1:
                out[name] = distinct[:6]
            continue
        else:
            continue
        if f.get("regex") is not None:
            try:
                pat = re.compile(f["regex"])
            except re.error:
                continue
            refined = []
            for v in values:
                m = pat.search(v)
                if m and m.groups() and (m.group(1) or "").strip():
                    refined.append(normalize_ws(m.group(1)))
            values = refined
        distinct = list(dict.fromkeys(v for v in values if v))
        if len(distinct) > 1:
            out[name] = distinct[:6]
    return out


# ---- TEACH from example pages: readers a page offers, checked, and a name-similarity guess ----------
# Domain-free: the destination's own field names are matched against the page's JSON-LD keys, meta tag names
# and its single <h1>. Every reader returned has passed `check_reader` (evidence, type rule, literal capture,
# ambiguity) on the page, so a guess is never a value that is not on the page.

# Boilerplate JSON-LD objects: tried after the page's main object (a breadcrumb's `name` is not the item's name).
_BOILERPLATE_TYPES = {"BreadcrumbList", "ListItem", "ItemList", "WebSite", "WebPage", "Organization", "SearchAction",
                      "SiteNavigationElement", "ImageObject", "EntryPoint", "ReadAction", "Brand", "Offer", "Person"}
_META_PREFIXES = ("og:", "twitter:", "citation_", "product:", "article:", "dc.", "DC.")
_META_SKIP = re.compile(r"^(og:image.*|og:url|og:locale|og:type|twitter:card|twitter:image.*|twitter:site|"
                        r"citation_pdf_url|citation_abstract_html_url)$")
# Name similarity: words that name the same thing on most pages, whatever they are about.
_CONCEPTS = {
    "title": {"title", "name", "product", "item", "headline", "heading"},
    "price": {"price", "cost", "amount"},
    "year": {"year"},
    "date": {"date", "published", "released", "release", "datepublished", "releasedate", "startdate", "publicationdate"},
    "venue": {"venue", "journal", "conference", "periodical", "ispartof", "proceedings"},
    "author": {"author", "authors", "creator", "byline"},
    "image": {"image", "photo", "picture", "thumbnail"},
    "description": {"description", "summary", "abstract"},
    "brand": {"brand", "manufacturer", "maker"},
}
_GENERIC_LEAVES = {"name", "value", "text", "id", "title"}


def _words(s: str) -> list[str]:
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(s or ""))
    return [w for w in re.split(r"[^a-z0-9]+", s.lower()) if w]


def _key_of_path(path: str) -> str:
    """`offers.price` -> price, `isPartOf.name` -> isPartOf name, `author[0].name` -> author name."""
    parts = [re.sub(r"\[\d+\]", "", p) for p in path.split(".") if p]
    if len(parts) >= 2 and parts[-1].lower() in _GENERIC_LEAVES:
        return " ".join(parts[-2:])
    return parts[-1] if parts else path


def _concepts(words: list[str]) -> set[str]:
    joined = "".join(words)
    out = {c for c, syn in _CONCEPTS.items() if joined in syn or any(w in syn for w in words)}
    if "year" in out:
        out.discard("date")
    return out


def _dest_key_words(dest_field: str) -> list[str]:
    """`venue.name` -> [venue] (the leaf `name` says little), `Date Added` -> [date, added]."""
    parts = [p for p in re.split(r"\.", str(dest_field or "").replace("[]", "")) if p]
    if len(parts) >= 2 and parts[-1].lower() in _GENERIC_LEAVES:
        parts = parts[:-1]
    return _words(" ".join(parts[-1:])) if parts else []


def _json_leaves_all(obj: Any, prefix: str = "", depth: int = 0) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if depth > 3:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.startswith("@") or k in ("url", "image", "logo", "sameAs", "potentialAction", "target", "@id"):
                continue
            out += _json_leaves_all(v, f"{prefix}.{k}" if prefix else k, depth + 1)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:1]):  # the first entry only: a reader names one value
            out += _json_leaves_all(v, f"{prefix}[{i}]", depth + 1)
    elif isinstance(obj, str) and normalize_ws(obj):
        out.append((prefix, normalize_ws(obj)))
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out.append((prefix, str(obj)))
    return out


def reader_candidates(record: dict, html_field: str = "html") -> list[tuple[str, dict]]:
    """Every reader the page offers, with the key it is named by: JSON-LD leaves (the main object first),
    meta tags (og:, twitter:, citation_, product:, article:) and a single-valued <h1>. Unchecked."""
    html = record.get(html_field) or ""
    out: list[tuple[str, dict]] = []
    objs = [o for o in jsonld_objects(html) if _types_of(o)]
    objs.sort(key=lambda o: 1 if set(_types_of(o)) <= _BOILERPLATE_TYPES else 0)
    seen_types: set[str] = set()
    for obj in objs:
        typ = _types_of(obj)[0]
        if typ in seen_types:
            continue  # a reader reads the first object of its type
        seen_types.add(typ)
        for path, value in _json_leaves_all(obj):
            if value.startswith(("http://", "https://")) and not path.endswith(("url", "Url")):
                continue
            out.append((_key_of_path(path), {"jsonld": path, "jsonld_type": typ}))
    try:
        metas = LexborHTMLParser(html).css("meta")
    except Exception:  # noqa: BLE001
        metas = []
    for m in metas:
        attr_name = "property" if m.attributes.get("property") else "name"
        key = m.attributes.get(attr_name) or ""
        if not key.startswith(_META_PREFIXES) or _META_SKIP.match(key) or not normalize_ws(m.attributes.get("content") or ""):
            continue
        short = re.sub(r"^(og|twitter|citation|product|article|dc)[:_.]", "", key, flags=re.I)
        out.append((short, {"css": 'meta[%s="%s"]' % (attr_name, key), "attr": "content"}))
    if len(set(v for v in css_all(html, "h1") if v)) == 1:
        out.append(("h1 heading title", {"css": "h1"}))
    return out


def check_reader(reader: dict, record: dict, rules: list[dict] | None = None, html_field: str = "html",
                 text_field: str = "text") -> dict:
    """One reader on one page: {ok, value, detail}. ok = it reads one value, the value occurs on the page and
    passes `rules` (validate rules on the field name "value"), and a regex does not capture fixed text."""
    probe = {"id": "probe", "fields": {"value": reader}}
    out = run_page_parser(probe, record, html_field, text_field)
    if "error" in out:
        return {"ok": False, "value": None, "detail": out["error"]}
    ev = evidence(record, out, rules or [], parser=probe, html_field=html_field, text_field=text_field)[0]
    fixed = literal_capture(str(reader.get("regex") or ""))
    if fixed is not None:
        return {"ok": False, "value": out["value"], "detail": f"the regex captures the fixed text {fixed!r}"}
    if not ev["ok"]:
        return {"ok": False, "value": out["value"], "detail": ev.get("detail") or "no evidence"}
    amb = ambiguity(record, probe, html_field)
    if amb:
        return {"ok": False, "value": out["value"], "detail": f"the reader reads several values: {amb['value'][:4]}"}
    return {"ok": True, "value": out["value"], "detail": ""}


def type_rules(field: str, kind: str, values: list | None = None) -> list[dict]:
    """The validate rules that check a value fits a destination field's type (number, date, url, choices)."""
    if kind in ("number", "integer"):
        return [{"field": field, "rule": "number"}]
    if kind == "date":
        return [{"field": field, "rule": "date"}]
    if kind == "url":
        return [{"field": field, "rule": "url"}]
    if kind == "one_of" and values:
        return [{"field": field, "rule": "one_of", "values": list(values)}]
    return []


def _year_variant(reader: dict) -> dict:
    return {**reader, "regex": r"(\d{4})"} if not reader.get("regex") else reader


def guess_reader(dest_field: str, dest_type: str, page: dict) -> dict | None:
    """A reader for a destination field, from the page's JSON-LD keys, meta tags and <h1>, matched by name
    (Product/Name/Title -> name/og:title/h1; Price -> offers.price). The first candidate that passes
    `check_reader` with the field's type rule wins; None when nothing on the page fits."""
    words = _dest_key_words(dest_field)
    if not words:
        return None
    want = _concepts(words)
    key = "".join(words)
    rules = type_rules("value", dest_type)
    scored: list[tuple[int, int, dict]] = []
    for i, (cand_key, reader) in enumerate(reader_candidates(page)):
        cwords = _words(cand_key)
        have = _concepts(cwords)
        ckey = "".join(cwords)
        if key == ckey:
            score = 0
        elif want & have:
            score = 1
        elif difflib.SequenceMatcher(None, key, ckey).ratio() >= 0.8:
            score = 2
        elif "year" in want and have & {"date"}:
            reader, score = _year_variant(reader), 3  # a year read from a date (`2025-06-12` -> 2025)
        else:
            continue
        if want == {"title"} and reader.get("css") == "h1":
            score = 1
        scored.append((score, i, reader))
    for _, _, reader in sorted(scored, key=lambda x: (x[0], x[1])):
        if check_reader(reader, page, rules)["ok"]:
            return reader
    return None


# ---- D2 ------------------------------------------------------------------------------------------

_PARSER_ADD = re.compile(r"^/extract/parsers/(\d+|-)$")


def additive_parser_patch(active_spec: dict, new_spec: dict) -> bool:
    """True iff the JSON Patch active -> new only adds one parser under /extract/parsers."""
    ops = jsonpatch.make_patch(active_spec, new_spec).patch
    return len(ops) == 1 and ops[0]["op"] == "add" and bool(_PARSER_ADD.match(ops[0]["path"]))


def d2_decision(*, policy: str, tests_passed: bool, evidence_ok: bool, all_corroborated: bool,
                ambiguous: dict[str, list[str]] | list[str] | None, additive: bool,
                is_document: bool = False, cross_checks_passed: int = 0) -> tuple[bool, list[str]]:
    """The D2 rule (contracts V10). Returns (auto_deploy, reasons a person has to decide).
    A document has no title or meta tags to corroborate it (V18): it needs at least one declared cross-check to
    have passed on this document instead, so a layout with no total to check always goes to a person."""
    reasons = []
    if policy != "auto":
        reasons.append(f"learn_policy is {policy!r}")
    if not tests_passed:
        reasons.append("the version's tests did not pass")
    if not evidence_ok:
        reasons.append("an extracted value has no evidence on the page")
    if is_document:
        if cross_checks_passed < 1:
            reasons.append("no declared cross-check (a sum, product or count) passed on this document, so nothing "
                           "confirms the rows add up")
    elif not all_corroborated:
        reasons.append("a value is not corroborated by the page title, meta tags or JSON-LD")
    if ambiguous:
        reasons.append(f"ambiguous: {', '.join(sorted(ambiguous))}")
    if not additive:
        reasons.append("the change does more than add one parser")
    return not reasons, reasons


# ---- documents and rows (contracts V17, V18) ------------------------------------------------------------

def _meta_lines(record: dict) -> list[str]:
    meta = record.get("meta") or {}
    lines = [f"document {record.get('id')} · kind {record.get('kind')} · site {record.get('site')}",
             f"file: {json.dumps(_clip(str(record.get('name') or ''), 120))}"]
    for key in ("from", "subject"):
        if meta.get(key):
            lines.append(f"{key}: {json.dumps(_clip(normalize_ws(str(meta[key])), 160))}")
    others = [k for k in meta if k not in ("from", "subject", "error", "problem") and isinstance(meta[k], (str, int))]
    if others:
        lines.append("meta keys: " + ", ".join(sorted(str(k) for k in others)[:12]))
    for flag in record.get("flags") or []:
        lines.append(f"flag: {flag}")
    return lines


def document_outline(record: dict, max_chars: int = 4000) -> str:
    """The compact view of a document the learner gets instead of its bytes: the meta lines (sender, subject,
    file), the text lines numbered L1.., each table (`T<n> columns: [...] rows: N` and its first 3 rows) and, when
    there is HTML, the page outline. Never the whole document (the learner's budget is ~6 model calls)."""
    parts = ["\n".join(_meta_lines(record))]
    tables = record.get("tables") or []
    for n, t in enumerate(tables, start=1):
        head = t.get("header") or []
        rows = t.get("rows") or []
        block = [f"T{n} columns: {json.dumps(head, ensure_ascii=False)} rows: {len(rows)}"
                 + (f" ({t['where']})" if t.get("where") else "")]
        block += [f"  r{i}: {json.dumps(r, ensure_ascii=False)}" for i, r in enumerate(rows[:3], start=1)]
        parts.append("\n".join(block))
    text_lines = [ln for ln in str(record.get("text") or "").splitlines() if ln.strip()]
    if text_lines:
        parts.append("text lines (L<n>: a `lines` select reads these):\n" + "\n".join(
            f"  L{i}: {_clip(ln.strip(), 140)}" for i, ln in enumerate(text_lines[:60], start=1))
            + ("\n  …" if len(text_lines) > 60 else ""))
    out = "\n".join(parts)
    if (record.get("html") or "").strip() and len(out) < max_chars - 400:
        out += "\n" + page_outline({**record, "id": record.get("id"), "text": ""}, max_chars=max_chars - len(out) - 1)
    return out[:max_chars]


def run_document_parser(parser: dict, record: dict, html_field: str = "html", text_field: str = "text") -> dict:
    """A candidate on one document: `{"fields": {...}, "rows": [Row...], "skipped": [...]}`, or
    `{"error": "...", "fields": <the document fields that worked>}`. The document fields are read like a page's;
    the rows by `runner.rows.extract_rows`, the code the compiled parser runs."""
    if not isinstance(parser, dict):
        return {"error": "parser must be an object with `fields` and `rows`"}
    out = run_page_parser(parser, record, html_field, text_field)
    if "error" in out:
        return out
    result: dict[str, Any] = {"fields": out, "rows": [], "skipped": []}
    spec = parser.get("rows")
    if spec is None:
        return result
    skipped: list[dict] = []
    try:
        result["rows"] = extract_rows(record, spec, f"{parser.get('id') or 'parser'}.rows", html_field, text_field,
                                      skipped)
    except Quarantine as q:
        return {"error": q.detail or q.code, "fields": out}
    except (KeyError, TypeError, ValueError, re.error) as e:
        return {"error": f"{parser.get('id') or 'parser'}.rows: {type(e).__name__}: {e}", "fields": out}
    result["skipped"] = skipped
    return result


def _cross_rules(validate: list[dict]) -> list[dict]:
    return [r for r in validate or [] if r.get("rule") in ("sum_equals", "product_equals", "count_equals")]


def _in_row(value: Any, spec: dict, row: Row) -> bool:
    """The value was read from this row: the text it was cleaned from occurs in the row's text or one of its
    cells, case-insensitively, or (a number) some number in the row equals it."""
    if value in (None, ""):
        return True
    if spec.get("attr") is not None:
        return True  # an attribute (a link) is not in the row's text
    hay = normalize_ws(row.text + " " + " ".join(str(c) for c in row.cells.values())).lower()
    if any(f.lower() in hay for f in value_forms(value, spec)):
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return any(abs(n - float(value)) < 1e-9 for n in _numbers_in(hay))
    return False


def rows_checks(record: dict, parser: dict, rows: list[Row], validate: list[dict], *,
                skipped: list[dict] | None = None, doc_fields: dict | None = None,
                html_field: str = "html", text_field: str = "text") -> list[dict]:
    """The checks a rows parser passes before it is saved, in code. One item `{name, ok, detail}` each:

    - `values_in_own_row`  every value occurs in the row it was read from (not in another row, not made up)
    - `required_present`   at least 95% of the rows have each required field (`rows.require`, `required` rules)
    - `no_duplicate_rows`  no two rows have identical values
    - `skips_explained`    every skipped row is blank, a repeated header or an aggregate (and every dropped line is
      blank, repeated or declared `skip`); none is silently dropped
    - `row_count`          the selected region's rows minus the explained skips equal the rows read
    - `rules_hold`         the spec's per-row validation rules hold for every row
    - one item per declared cross-check rule (`sum_equals`, `product_equals`, `count_equals`), run on this
      document's fields (`doc_fields`, read from the parser when not given) and rows"""
    spec = parser.get("rows") or {}
    specs = spec.get("fields") or {}
    out: list[dict] = []

    def item(name: str, ok: bool, detail: str = "") -> None:
        out.append({"name": name, "ok": bool(ok), **({"detail": detail} if detail else {})})

    bad = next(((r, f, r.fields.get(f)) for r in rows for f in specs
                if not _in_row(r.fields.get(f), specs[f], r)), None)
    item("values_in_own_row", bad is None,
         "" if bad is None else f"row {bad[0].no}: {bad[1]}={bad[2]!r} does not occur in that row ({_clip(bad[0].text, 80)!r})")

    required = list(dict.fromkeys([*(spec.get("require") or [next(iter(specs), "")]),
                                   *(r["field"] for r in validate or [] if r.get("rule") == "required")]))
    missing = []
    for f in [f for f in required if f in specs]:
        n = sum(1 for r in rows if r.fields.get(f) not in (None, ""))
        if rows and n / len(rows) < 0.95:
            missing.append(f"{f} is empty in {len(rows) - n} of {len(rows)} rows")
    item("required_present", not missing, "; ".join(missing))

    seen: dict[tuple, int] = {}
    dup = None
    for r in rows:
        key = tuple(sorted((k, str(v)) for k, v in r.fields.items()))
        if key in seen:
            dup = (seen[key], r.no)
            break
        seen[key] = r.no
    item("no_duplicate_rows", dup is None, "" if dup is None else f"rows {dup[0]} and {dup[1]} have identical values")

    if skipped is None:
        skipped = []
        try:
            extract_rows(record, spec, f"{parser.get('id') or 'parser'}.rows", html_field, text_field, skipped)
        except Quarantine as q:
            item("skips_explained", False, q.detail or q.code)
            skipped = None
    if skipped is not None:
        dropped = [x for x in skipped if "row" in x]
        by_reason: dict[str, int] = {}
        for x in skipped:
            by_reason[x["reason"]] = by_reason.get(x["reason"], 0) + 1
        item("skips_explained", all(x.get("reason") for x in skipped),
             "; ".join(f"{n} {why}" for why, n in by_reason.items()) if by_reason else "")
        try:
            selected = len(select_rows(record, spec["select"], html_field, text_field,
                                       f"{parser.get('id') or 'parser'}.rows"))
        except Quarantine:
            selected = len(rows) + len(dropped)
        item("row_count", selected - len(dropped) == len(rows),
             f"{selected} selected - {len(dropped)} skipped = {selected - len(dropped)}, but {len(rows)} rows were read")

    broken = None
    for r in rows:
        for f, v in r.fields.items():
            problem = _check_rules(f, v, [x for x in validate or [] if x.get("rule") not in
                                          ("sum_equals", "product_equals", "count_equals")])
            if problem and broken is None:
                broken = f"row {r.no}: {f}={v!r} fails validation: {problem}"
    item("rules_hold", broken is None, broken or "")
    if _cross_rules(validate):
        if doc_fields is None:
            got = run_page_parser(parser, record, html_field, text_field)
            doc_fields = got if "error" not in got else got.get("fields", {})
        out += cross_check_items(doc_fields, rows, validate)
    return out


def cross_check_items(parser_fields: dict, rows: list[Row], validate: list[dict]) -> list[dict]:
    """One `{name, ok, detail}` per declared cross-check rule on this document (the plan's `sum_equals` etc.)."""
    items = []
    rules = _cross_rules(validate)
    for rule in rules:
        problem = check_cross(rule, parser_fields, rows)
        name = rule["rule"] if sum(1 for r in rules if r["rule"] == rule["rule"]) == 1 else f"{rule['rule']}:{rule['field']}"
        items.append({"name": name, "ok": problem is None, "cross": True, **({"detail": problem} if problem else {})})
    return items


__all__ = ["reader_candidates", "check_reader", "type_rules", "guess_reader", "integration_fields", "integration_jsonld_types", "known_readers", "rule_label", "page_outline", "jsonld_types", "infer_jsonld_type", "with_jsonld_types", "run_page_parser", "evidence", "corroborated", "ambiguity", "additive_parser_patch",
           "d2_decision", "make_page_record", "page_text", "extractor_label", "extract_field", "css_path",
           "corroboration_sources", "PAGE_METHODS", "document_outline", "run_document_parser", "rows_checks",
           "cross_check_items", "is_document", "value_forms"]
