"""Spike B: can one deterministic reader per platform extract title + company from rendered job pages?

Usage: python -I spike_b.py <fixtures/pages dir>
Extractor vocabulary (closed):
  {"css": sel, "attr": name?}          first match; attr value, else whitespace-normalised text
  {"jsonld": "a.b.c"}                  dotted path into the schema.org JobPosting (arrays + @graph handled)
  {"regex": pat}                       one capture group over visible text
  any of css/jsonld + {"refine": pat}  one capture group applied to the css/jsonld result
"""
import json, re, sys, time, pathlib
from selectolax.lexbor import LexborHTMLParser

WS = re.compile(r"\s+")
norm = lambda s: WS.sub(" ", s or "").strip()


def visible_text(tree):
    body = tree.body
    if body is None:
        return ""
    clone = LexborHTMLParser(body.html)
    for n in clone.css("script, style, noscript, template"):
        n.decompose()
    return norm(clone.body.text(separator=" "))


def find_jobposting(tree):
    def walk(o):
        if isinstance(o, list):
            for x in o:
                r = walk(x)
                if r: return r
        elif isinstance(o, dict):
            t = o.get("@type")
            if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                return o
            if "@graph" in o:
                return walk(o["@graph"])
        return None
    for s in tree.css('script[type="application/ld+json"]'):
        try:
            r = walk(json.loads(s.text()))
        except ValueError:
            continue
        if r: return r
    return None


def jsonld_get(obj, path):
    for k in path.split("."):
        if isinstance(obj, list):
            obj = obj[0] if obj else None
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    if isinstance(obj, (str, int, float)):
        return norm(str(obj))
    return None


def extract(spec, tree, text, jp):
    if "css" in spec:
        m = tree.css_first(spec["css"])
        v = None if m is None else (m.attributes.get(spec["attr"]) if "attr" in spec else m.text(separator=" "))
        v = norm(v) if v is not None else None
    elif "jsonld" in spec:
        v = jsonld_get(jp, spec["jsonld"]) if jp else None
    elif "regex" in spec:
        m = re.search(spec["regex"], text)
        v = norm(m.group(1)) if m else None
    else:
        raise ValueError(spec)
    if v is not None and "refine" in spec:
        m = re.search(spec["refine"], v)
        v = norm(m.group(1)) if m else None
    return v or None


READERS = {
    "lever": {"title": {"css": ".posting-headline h2"},
              "company": {"jsonld": "hiringOrganization.name"}},
    "greenhouse": {"title": {"css": ".job__title h1"},
                   "company": {"css": "title", "refine": r"^Job Application for .* at (.+)$"}},
    "greenhouse_branded": {"title": {"css": "h1"},
                           "company": {"css": 'meta[property="og:site_name"]', "attr": "content",
                                       "refine": r"^Careers at (.+)$"}},
    "ashby": {"title": {"jsonld": "title"},
              "company": {"jsonld": "hiringOrganization.name"}},
}
# Alternatives tried without JSON-LD / without refine, to answer "is refine needed?"
ALTERNATIVES = {
    "lever": {"company_no_jsonld": {"css": ".main-header-logo img", "attr": "alt", "refine": r"^(.*) logo$"},
              "title_jsonld": {"jsonld": "title"}},
    "greenhouse": {"company_logo_alt": {"css": ".logo img", "attr": "alt", "refine": r"^(.*) Logo$"},
                   "company_logo_alt_raw": {"css": ".logo img", "attr": "alt"}},
    "greenhouse_branded": {"company_jsonld": {"jsonld": "hiringOrganization.name"}},
    "ashby": {"title_css": {"css": "h1.ashby-job-posting-heading"},
              "title_css_h1": {"css": "h1"},
              "company_css_img": {"css": "img[alt]", "attr": "alt"}},
}
FINGERPRINT_CANDIDATES = [
    ".posting-headline", 'a[href*="lever.co"]', 'img[alt="Lever logo"]', '[src*="lever"]',
    '[src*=".greenhouse.io/"]', '[href*=".greenhouse.io/"]', 'iframe[src*="greenhouse.io/embed/job_app"]',
    '[href*="greenhouse.io"], [src*="greenhouse.io"]', ".job__title",
    '[class*="ashby-job-posting"]', 'meta[property="og:image"][content*="ashbyhq.com"]', '[src*="ashbyhq.com"]',
]
FP_GROUP = {"lever": "lever", "greenhouse": "greenhouse", "greenhouse_branded": "greenhouse", "ashby": "ashby"}


def main(d):
    d = pathlib.Path(d)
    index = json.loads((d / "index.json").read_text())
    pages, results = [], []
    for e in index:
        html = (d / e["file"]).read_text()
        t0 = time.perf_counter()
        tree = LexborHTMLParser(html)
        text = visible_text(tree)
        jp = find_jobposting(tree)
        got = {f: extract(s, tree, text, jp) for f, s in READERS[e["platform"]].items()}
        ms = (time.perf_counter() - t0) * 1000
        alts = {k: extract(s, tree, text, jp) for k, s in ALTERNATIVES.get(e["platform"], {}).items()}
        amb = {}
        for f, s in READERS[e["platform"]].items():
            if "css" in s:
                ms_ = tree.css(s["css"])
                vals = {norm(m.attributes.get(s["attr"]) if "attr" in s else m.text(separator=" ")) for m in ms_}
                amb[f] = f"{len(ms_)} match, {len(vals)} distinct"
        pages.append((e, tree))
        results.append(dict(file=e["file"], ok={f: got[f] == e["expected"][f] for f in got}, got=got,
                            jsonld=jp is not None, ms=round(ms, 1), bytes=len(html),
                            verbatim={f: norm(e["expected"][f]) in text for f in ("title", "company")},
                            css_matches=amb, alts=alts))
    for r in results:
        print(json.dumps(r, ensure_ascii=False))
    print("\nFINGERPRINTS (present on every page of group, absent from all others):")
    for sel in FINGERPRINT_CANDIDATES:
        hits = {e["file"]: tree.css_first(sel) is not None for e, tree in pages}
        for g in sorted(set(FP_GROUP.values())):
            ins = [hits[e["file"]] for e, _ in pages if FP_GROUP[e["platform"]] == g]
            outs = [hits[e["file"]] for e, _ in pages if FP_GROUP[e["platform"]] != g]
            if all(ins) and not any(outs):
                print(f"  {g:10s} OK   {sel}")
        print(f"  {'':10s} hits {sel}: {[k for k, v in hits.items() if v]}")
    total = sum(r["ok"][f] for r in results for f in r["ok"])
    print(f"\ncorrect fields: {total}/{2 * len(results)}; parse+extract ms: "
          f"{[r['ms'] for r in results]}")


if __name__ == "__main__":
    main(sys.argv[1])
