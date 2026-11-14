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

import json
import re
from datetime import datetime, timezone
from typing import Any

import jsonpatch
import yaml
from selectolax.lexbor import LexborHTMLParser

from runner.html import (canonical_url, css_all, css_first, jsonld_objects, normalize_ws, site_of,
                         visible_text)
from runner.lib import (Quarantine, cast, extract_between, extract_css, extract_json_path, extract_jsonld,
                        extract_regex, refine_regex, rule_problem_date, rule_problem_number, rule_problem_url)

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
    """One field, exactly as the generated parser does it (runner.lib helpers only)."""
    html = record.get(html_field) or ""
    if not isinstance(f, dict):
        raise Quarantine("PARSE_NO_MATCH", f"{where}: extractor must be a mapping")
    if f.get("css") is not None:
        value: Any = extract_css(html, str(f["css"]), f.get("attr"), where)
    elif f.get("jsonld") is not None:
        value = extract_jsonld(html, str(f["jsonld"]), f.get("jsonld_type") or "JobPosting", where)
    elif f.get("attr") is not None or f.get("jsonld_type") is not None:
        raise Quarantine("PARSE_NO_MATCH", f"{where}: attr needs css and jsonld_type needs jsonld")
    elif f.get("regex") is not None:
        value = extract_regex(page_text(record, text_field), re.compile(f["regex"]), where)
        return cast(value, f.get("cast", "str"), where)
    elif f.get("json_path") is not None:
        value = extract_json_path(record, f["json_path"], where)
    elif f.get("between") is not None:
        value = extract_between(page_text(record, text_field), f["between"][0], f["between"][1], where)
    else:
        raise Quarantine("PARSE_NO_MATCH", f"{where}: no extract method (use css, jsonld, regex, json_path or between)")
    if f.get("regex") is not None:
        value = refine_regex(str(value), re.compile(f["regex"]), where)
    return cast(value, f.get("cast", "str"), where)


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


def evidence(record: dict, fields: dict, validate: list[dict], parser: dict | None = None,
             html_field: str = "html", text_field: str = "text") -> list[dict]:
    """V10 evidence items: {name, value, ok, where: text|jsonld|html|None, extractor}. A value is
    evidenced if it occurs (whitespace-normalised) in the visible text, a JSON-LD string, an attribute
    value or the <title>, and it passes the spec's validation rules. record.* values never reach here."""
    html = record.get(html_field) or ""
    text = normalize_ws(page_text(record, text_field))
    lazy: dict[str, list[str]] = {}
    items = []
    for name, value in fields.items():
        v = normalize_ws(str(value)) if value is not None else ""
        where = None
        if v:
            if v in text:
                where = "text"
            else:
                if "jsonld" not in lazy:
                    lazy["jsonld"] = _jsonld_strings(html)
                if any(v in s for s in lazy["jsonld"]):
                    where = "jsonld"
                else:
                    if "html" not in lazy:
                        lazy["html"] = _attr_values(html) + [t for t in css_all(html, "head title") if t]
                    if any(v in s for s in lazy["html"]):
                        where = "html"
        rule_problem = _check_rules(name, value, validate)
        item = {"name": name, "value": value, "ok": where is not None and rule_problem is None, "where": where,
                "extractor": extractor_label((parser or {}).get("fields", {}).get(name) or {})}
        if where is None:
            item["detail"] = "value does not occur on the page"
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


# ---- D2 ------------------------------------------------------------------------------------------

_PARSER_ADD = re.compile(r"^/extract/parsers/(\d+|-)$")


def additive_parser_patch(active_spec: dict, new_spec: dict) -> bool:
    """True iff the JSON Patch active -> new only adds one parser under /extract/parsers."""
    ops = jsonpatch.make_patch(active_spec, new_spec).patch
    return len(ops) == 1 and ops[0]["op"] == "add" and bool(_PARSER_ADD.match(ops[0]["path"]))


def d2_decision(*, policy: str, tests_passed: bool, evidence_ok: bool, all_corroborated: bool,
                ambiguous: dict[str, list[str]] | list[str] | None, additive: bool) -> tuple[bool, list[str]]:
    """The D2 rule (contracts V10). Returns (auto_deploy, reasons a person has to decide)."""
    reasons = []
    if policy != "auto":
        reasons.append(f"learn_policy is {policy!r}")
    if not tests_passed:
        reasons.append("the version's tests did not pass")
    if not evidence_ok:
        reasons.append("an extracted value has no evidence on the page")
    if not all_corroborated:
        reasons.append("a value is not corroborated by the page title, meta tags or JSON-LD")
    if ambiguous:
        reasons.append(f"ambiguous: {', '.join(sorted(ambiguous))}")
    if not additive:
        reasons.append("the change does more than add one parser")
    return not reasons, reasons


__all__ = ["integration_fields", "integration_jsonld_types", "known_readers", "rule_label", "page_outline", "jsonld_types", "infer_jsonld_type", "with_jsonld_types", "run_page_parser", "evidence", "corroborated", "ambiguity", "additive_parser_patch",
           "d2_decision", "make_page_record", "page_text", "extractor_label", "extract_field", "css_path",
           "corroboration_sources", "PAGE_METHODS"]
