"""Deterministic page helpers (docs/contracts.md Part 2, V2). Pure functions, no network, no AI.

Used by generated parsers (via runner.lib), the control plane's page inbox and the agents' checks, so
everyone reads a page the same way. Unparseable HTML never raises: functions return empty results.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from selectolax.lexbor import LexborHTMLParser

_SKIP_TAGS = ["script", "style", "noscript", "template", "svg"]
_BLOCK = ("address,article,aside,blockquote,br,dd,div,dl,dt,fieldset,figcaption,figure,footer,form,h1,h2,h3,h4,h5,"
          "h6,header,hr,label,li,main,nav,ol,option,p,pre,section,table,td,th,tr,ul,button")
_DROP_PARAMS = re.compile(r"^(utm_.*|gh_src|lever-source|source|ref)$", re.I)
_PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def normalize_ws(s: str) -> str:
    return " ".join((s or "").split())


@lru_cache(maxsize=8)
def _tree(html: str) -> LexborHTMLParser:
    # Cached because one record is read by several extractors in a row. Callers must not mutate it.
    return LexborHTMLParser(html or "")


def visible_text(html: str) -> str:
    """Body text without script/style/noscript/template/svg; one line per block element, each line
    whitespace-normalised, empty lines dropped."""
    if not html:
        return ""
    try:
        tree = LexborHTMLParser(html)  # a private copy: this function edits the tree
        tree.strip_tags(_SKIP_TAGS)
        for node in tree.css(_BLOCK):
            node.insert_before("\n")
            node.insert_after("\n")
        root = tree.body or tree.root
        raw = root.text(deep=True, separator="") if root is not None else ""
    except Exception:  # noqa: BLE001 - never raise on bad pages
        return ""
    return "\n".join(line for line in (normalize_ws(x) for x in raw.splitlines()) if line)


def site_of(url: str) -> str:
    try:
        host = urlsplit(url or "").hostname or ""
    except ValueError:
        return ""
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


# Job platforms that give each company its own subdomain (acme.wd5.myworkdayjobs.com, acme.bamboohr.com): every
# tenant serves the same layout, so a parser can match the platform (`site: "*.myworkdayjobs.com"`) instead of one
# company's host. Platforms with the company in the path (jobs.lever.co/acme) need nothing: the host is the platform.
TENANT_PLATFORMS = {"myworkdayjobs.com": "workday", "myworkdaysite.com": "workday", "icims.com": "icims",
                    "taleo.net": "taleo", "bamboohr.com": "bamboohr", "recruitee.com": "recruitee", "breezy.hr": "breezy",
                    "teamtailor.com": "teamtailor", "pinpointhq.com": "pinpoint", "applytojob.com": "jazzhr",
                    "jobs.personio.de": "personio", "jobs.personio.com": "personio"}


def platform_of(site: str) -> str | None:
    """`visa.wd5.myworkdayjobs.com` -> `*.myworkdayjobs.com` (the wildcard a platform parser matches); a host
    that is not a tenant of a known multi-tenant platform -> None."""
    host = (site or "").lower()
    for suffix in TENANT_PLATFORMS:
        if host.endswith("." + suffix):
            return "*." + suffix
    return None


def platform_name(platform: str) -> str | None:
    """`*.myworkdayjobs.com` -> `workday` (the id a platform parser gets)."""
    return TENANT_PLATFORMS.get(platform.removeprefix("*.")) if platform else None


def _clean_url(url: str) -> str:
    parts = urlsplit(url)
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _DROP_PARAMS.match(k)])
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, query, ""))


def canonical_url(url: str, html: str) -> str:
    """<link rel=canonical> if it resolves to an absolute http(s) URL (upgraded to https when the page itself
    was https on the same host); else the URL itself. Either way
    without fragment and without tracking params (utm_*, gh_src, lever-source, source, ref)."""
    href = css_first(html, 'link[rel="canonical"]', "href")
    if href:
        candidate = urljoin(url or "", href)
        parts, page = urlsplit(candidate), urlsplit(url or "")
        if parts.scheme in ("http", "https"):
            if parts.scheme == "http" and page.scheme == "https" and parts.netloc == page.netloc:
                candidate = parts._replace(scheme="https").geturl()  # Greenhouse's canonical says http://
            return _clean_url(candidate)
    try:
        return _clean_url(url or "")
    except ValueError:
        return url or ""


def css_all(html: str, selector: str, attr: str | None = None) -> list[str]:
    """normalize_ws'd text (or attribute value) of every match, in document order."""
    try:
        nodes = _tree(html).css(selector)
    except Exception:  # noqa: BLE001 - bad selector or bad page
        return []
    out = []
    for n in nodes:
        if attr:
            v = n.attributes.get(attr)
            if v is not None:
                out.append(normalize_ws(v))
        else:
            out.append(normalize_ws(n.text(deep=True, separator=" ")))
    return out


def css_first(html: str, selector: str, attr: str | None = None) -> str | None:
    values = css_all(html, selector, attr)
    return values[0] if values else None


def _flatten(obj: Any) -> list[dict]:
    if isinstance(obj, list):
        return [o for item in obj for o in _flatten(item)]
    if isinstance(obj, dict):
        graph = obj.get("@graph")
        return [obj] + (_flatten(graph) if isinstance(graph, list) else [])
    return []


def jsonld_objects(html: str) -> list[dict]:
    """Every object in every ld+json script; arrays and @graph flattened."""
    try:
        scripts = _tree(html).css('script[type="application/ld+json"]')
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []
    for s in scripts:
        try:
            out += _flatten(json.loads(s.text(deep=True) or "null"))
        except (ValueError, TypeError):
            continue
    return out


def _has_type(obj: dict, type_: str) -> bool:
    t = obj.get("@type")
    return t == type_ or (isinstance(t, list) and type_ in t)


def jsonld_get(html: str, path: str, type_: str = "JobPosting") -> Any | None:
    """Dotted path with [n] (e.g. hiringOrganization.name, identifier[0].value) on the first JSON-LD
    object of @type `type_`. None if there is no such object or the path is absent."""
    obj = next((o for o in jsonld_objects(html) if _has_type(o, type_)), None)
    if obj is None:
        return None
    cur: Any = obj
    for name, idx in _PATH_TOKEN.findall(path or ""):
        try:
            cur = cur[int(idx)] if idx else cur[name]
        except (KeyError, IndexError, TypeError):
            return None
    return cur


def fingerprint_present(html: str, selector: str) -> bool:
    try:
        return _tree(html).css_first(selector) is not None
    except Exception:  # noqa: BLE001
        return False
