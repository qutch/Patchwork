"""Fetch real, public job postings as rendered DOM for the v2 benchmark (W5-1, docs/contracts.md V12).

    python scripts/fetch_postings.py list    # 1 public listing request per board -> fixtures/postings/plan.json
    python scripts/fetch_postings.py render  # headless Chromium, one navigation per posting -> <platform>/<n>.html.gz
    python scripts/fetch_postings.py key     # offline: build answer_key.json from the stored pages + listing data

Politeness: sequential, at least MIN_GAP_S between any two top-level requests (listing calls and page
navigations), a normal desktop Chrome UA, images/fonts/media not downloaded, and a hard cap of
MAX_REQUESTS top-level requests per invocation. Pages behind a login, a captcha or a bot wall are skipped
and logged, never worked around.

Answer key (the `key` step, no network): for each page, title and company are read from independent
sources and compared after whitespace normalisation:
  - the ATS's own listing API (fetched in `list`: title, and company where the API has it),
  - schema.org JobPosting JSON-LD in the page (title, hiringOrganization.name),
  - the visible headings (h1/h2) and the document <title> / og:title / og:site_name.
The value is accepted when the listing title equals JSON-LD's title or a visible heading, and it occurs in
the page's visible text. Anything else is listed in `notes` and resolved by reading the page (see
RESOLVED below); each entry records which checks agreed (`checks`) and how it was decided (`method`).
"""

from __future__ import annotations

import gzip
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from runner.html import css_all, jsonld_get, normalize_ws, site_of, visible_text

OUT = REPO / "fixtures" / "postings"
PLAN = OUT / "plan.json"
KEY = OUT / "answer_key.json"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")
MIN_GAP_S = 1.2
MAX_REQUESTS = 150

# platform -> boards; per board, how many postings to take. ~12 per platform, 5 platforms.
BOARDS: dict[str, list[tuple[str, int]]] = {
    "lever": [("palantir", 6), ("spotify", 6)],  # plaid, mistral: 404 on the Lever API on 2026-11-14
    "greenhouse": [("discord", 4), ("anthropic", 4), ("figma", 4)],
    "greenhouse_branded": [("airbnb", 12)],
    "ashby": [("ramp", 4), ("openai", 4), ("notion", 4)],
    "smartrecruiters": [("BoschGroup", 12)],  # Visa: 0 postings listed on 2026-11-14
}

BLOCK_MARKERS = ("just a moment", "verify you are human", "captcha", "access denied", "attention required",
                 "please enable cookies", "unusual traffic")


class Budget:
    def __init__(self, cap: int = MAX_REQUESTS, gap: float = MIN_GAP_S):
        self.cap, self.gap, self.used, self._last = cap, gap, 0, 0.0

    def take(self, what: str) -> None:
        if self.used >= self.cap:
            raise SystemExit(f"request cap {self.cap} reached before {what}")
        wait = self._last + self.gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        self.used += 1
        print(f"[{self.used:3d}] {what}", flush=True)


# ---------------------------------------------------------------------------------------------
# list: one public listing request per board

def _pick(items: list[dict], n: int) -> list[dict]:
    """Deterministic, spread over the board, distinct titles: every k-th posting."""
    seen, uniq = set(), []
    for it in items:
        t = normalize_ws(it["api_title"]).lower()
        if t and t not in seen:
            seen.add(t)
            uniq.append(it)
    if len(uniq) <= n:
        return uniq
    step = len(uniq) / n
    return [uniq[int(i * step)] for i in range(n)]


def list_board(http: httpx.Client, budget: Budget, platform: str, board: str) -> list[dict]:
    if platform == "lever":
        budget.take(f"GET lever postings {board}")
        r = http.get(f"https://api.lever.co/v0/postings/{board}", params={"mode": "json"})
        r.raise_for_status()
        return [{"url": j["hostedUrl"], "api_title": j["text"], "api_company": None, "board": board}
                for j in r.json()]
    if platform in ("greenhouse", "greenhouse_branded"):
        budget.take(f"GET greenhouse board {board}")
        r = http.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs")
        r.raise_for_status()
        out = []
        for j in r.json().get("jobs", []):
            # hosted: build the job-boards URL ourselves; branded: follow the company's absolute_url
            url = (f"https://job-boards.greenhouse.io/{board}/jobs/{j['id']}" if platform == "greenhouse"
                   else j["absolute_url"])
            out.append({"url": url, "api_title": j["title"], "api_company": j.get("company_name"), "board": board})
        return out
    if platform == "ashby":
        budget.take(f"GET ashby job-board {board}")
        r = http.get(f"https://api.ashbyhq.com/posting-api/job-board/{board}")
        r.raise_for_status()
        return [{"url": j["jobUrl"], "api_title": j["title"], "api_company": None, "board": board}
                for j in r.json().get("jobs", []) if j.get("isListed", True)]
    if platform == "smartrecruiters":
        budget.take(f"GET smartrecruiters postings {board}")
        r = http.get(f"https://api.smartrecruiters.com/v1/companies/{board}/postings", params={"limit": 100})
        r.raise_for_status()
        return [{"url": f"https://jobs.smartrecruiters.com/{board}/{j['id']}", "api_title": j["name"],
                 "api_company": (j.get("company") or {}).get("name"), "board": board}
                for j in r.json().get("content", [])]
    raise ValueError(platform)


def cmd_list() -> None:
    budget = Budget()
    prev = json.loads(PLAN.read_text()) if PLAN.exists() else {"postings": [], "skipped": [], "requests": 0}
    have = {(p["platform"], p["board"]) for p in prev["postings"]}
    plan: list[dict] = []
    skipped: list[dict] = list(prev["skipped"])
    with httpx.Client(headers={"User-Agent": UA, "Accept": "application/json"}, timeout=30,
                      follow_redirects=True) as http:
        for platform, boards in BOARDS.items():
            for board, n in boards:
                if (platform, board) in have:  # listed before: keep that pick, re-take only if n grew
                    kept = [p for p in prev["postings"] if (p["platform"], p["board"]) == (platform, board)]
                    if len(kept) >= n:
                        plan += kept[:n]
                        continue
                try:
                    items = list_board(http, budget, platform, board)
                except (httpx.HTTPError, ValueError, KeyError) as e:
                    skipped.append({"platform": platform, "board": board, "why": f"listing failed: {e}"[:200]})
                    print(f"    skip {platform}/{board}: {e}"[:200])
                    continue
                picked = _pick(items, n)
                print(f"    {platform}/{board}: {len(items)} listed, {len(picked)} picked")
                plan += [{"platform": platform, **p} for p in picked]
    OUT.mkdir(parents=True, exist_ok=True)
    PLAN.write_text(json.dumps({"listed_at": _now(), "requests": prev["requests"] + budget.used, "postings": plan,
                                "skipped": skipped}, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(plan)} postings planned, {budget.used} requests -> {PLAN.relative_to(REPO)}")


# ---------------------------------------------------------------------------------------------
# render: headless Chromium, rendered DOM

def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _blocked(html: str, title: str) -> str | None:
    head = (title + " " + visible_text(html)[:1500]).lower()
    for m in BLOCK_MARKERS:
        if m in head:
            return f"looks blocked ({m!r})"
    return None


def cmd_render(only: str | None = None) -> None:
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    plan = json.loads(PLAN.read_text())
    log_path = OUT / "fetch_log.json"
    log = json.loads(log_path.read_text()) if log_path.exists() else {"pages": [], "skipped": []}
    done = {p["url"] for p in log["pages"]}
    counters: dict[str, int] = {}
    for p in log["pages"]:
        counters[p["platform"]] = max(counters.get(p["platform"], 0), int(Path(p["file"]).name.split(".")[0]))
    budget = Budget()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 900}, locale="en-US")
        ctx.route("**/*", lambda route: route.abort() if route.request.resource_type in ("image", "media", "font")
                  else route.continue_())
        for item in plan["postings"]:
            if item["url"] in done or (only and item["platform"] != only):
                continue
            platform = item["platform"]
            budget.take(f"render {item['url']}")
            page = ctx.new_page()
            try:
                try:
                    page.goto(item["url"], wait_until="networkidle", timeout=30000)
                except PWTimeout:
                    page.wait_for_timeout(3000)  # long-polling pages never go idle; take what rendered
                try:
                    page.wait_for_selector("h1, h2", timeout=8000)
                except PWTimeout:
                    pass
                html = page.content()
                title = page.title()
                final = page.url
            except Exception as e:  # noqa: BLE001 - log and move on, never retry aggressively
                log["skipped"].append({**item, "why": f"render failed: {e}"[:300]})
                print(f"    skip: {e}"[:200])
                continue
            finally:
                page.close()
            why = _blocked(html, title)
            if why:
                log["skipped"].append({**item, "why": why, "final_url": final})
                print(f"    skip: {why}")
                continue
            counters[platform] = counters.get(platform, 0) + 1
            rel = f"{platform}/{counters[platform]}.html.gz"
            dest = OUT / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(gzip.compress(html.encode("utf-8"), mtime=0))
            log["pages"].append({**item, "file": rel, "final_url": final, "document_title": title,
                                 "fetched_at": _now(), "bytes": len(html)})
            log_path.write_text(json.dumps(log, indent=2, ensure_ascii=False) + "\n")
            print(f"    ok -> {rel} ({len(html) // 1024} KB) {title[:70]!r}")
        browser.close()
    log_path.write_text(json.dumps(log, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(log['pages'])} pages stored, {len(log['skipped'])} skipped, {budget.used} requests this run")


# ---------------------------------------------------------------------------------------------
# key: offline answer key

# Disagreements between sources, resolved by reading the page. file -> {field: value, "why": text}
RESOLVED: dict[str, dict[str, str]] = {
    "lever/9.html.gz": {"title": "Head of Measurement & Advertising Effectiveness",
                        "why": "JSON-LD title is HTML-escaped ('&amp;'); the visible h2, <title>, og:title and the "
                               "listing API all say '&'"},
}
CHECKED_BY = ("scripts/fetch_postings.py cross-check of independent sources (listing API, JSON-LD, visible headings, "
              "<title>/og meta); the one disagreement was read and resolved by the W5 builder (an AI agent, not a "
              "human) from those sources and is listed in `notes`")

# Company per board when neither the listing API nor JSON-LD names it: the employer name the page itself
# shows (logo alt / <title> / og:site_name), checked in `key` to occur on the page.
BOARD_COMPANY: dict[str, str] = {}


def load_html(rel: str) -> str:
    return gzip.decompress((OUT / rel).read_bytes()).decode("utf-8")


def _eq(a: Any, b: Any) -> bool:
    return isinstance(a, str) and isinstance(b, str) and normalize_ws(a).lower() == normalize_ws(b).lower() != ""


def sources(html: str, document_title: str) -> dict[str, Any]:
    ld_title = jsonld_get(html, "title")
    ld_company = jsonld_get(html, "hiringOrganization.name")
    return {
        "jsonld_title": normalize_ws(ld_title) if isinstance(ld_title, str) else None,
        "jsonld_company": normalize_ws(ld_company) if isinstance(ld_company, str) else None,
        "h1": list(dict.fromkeys(v for v in css_all(html, "h1") if v))[:4],
        "h2": list(dict.fromkeys(v for v in css_all(html, "h2") if v))[:4],
        "document_title": normalize_ws(document_title),
        "og_title": next(iter(css_all(html, 'meta[property="og:title"]', "content")), None),
        "og_site_name": next(iter(css_all(html, 'meta[property="og:site_name"]', "content")), None),
    }


def decide(entry: dict, html: str) -> dict:
    text = visible_text(html)
    text_l = normalize_ws(text).lower()
    src = sources(html, entry.get("document_title") or "")
    api_title = normalize_ws(entry["api_title"])
    checks: dict[str, bool] = {
        "title_api_eq_jsonld": _eq(api_title, src["jsonld_title"]),
        "title_api_in_headings": any(_eq(api_title, h) for h in src["h1"] + src["h2"]),
        "title_in_visible_text": api_title.lower() in text_l,
    }
    notes: list[str] = []
    title, method = api_title, None
    if (checks["title_api_eq_jsonld"] or checks["title_api_in_headings"]) and checks["title_in_visible_text"]:
        method = "listing API title == " + ("JSON-LD title" if checks["title_api_eq_jsonld"] else "visible heading")
    company = None
    for cand, how in ((src["jsonld_company"], "JSON-LD hiringOrganization.name"),
                      (entry.get("api_company"), "listing API company"),
                      (BOARD_COMPANY.get(f"{entry['platform']}/{entry['board']}"), "board employer name")):
        if cand:
            company, company_how = normalize_ws(cand), how
            break
    if company is None:
        company_how = None
    checks["company_in_page"] = bool(company) and (company.lower() in text_l or company.lower() in html.lower())
    meta = " ".join(str(x) for x in (src["document_title"], src["og_title"], src["og_site_name"], src["jsonld_company"]) if x)
    checks["company_in_title_or_meta"] = bool(company) and company.lower() in meta.lower()
    # traps a learned reader could fall into, recorded so a wrong row can be explained
    if src["jsonld_title"] and not _eq(src["jsonld_title"], api_title):
        notes.append(f"trap: JSON-LD title is {src['jsonld_title']!r}, not the visible title")
    others = [h for h in src["h1"] if not _eq(h, api_title)]
    if others and any(_eq(h, api_title) for h in src["h1"]):
        notes.append(f"trap: other h1 on the page: {others}")
    if company and entry.get("api_company") and src["jsonld_company"]:
        checks["company_api_eq_jsonld"] = _eq(entry["api_company"], src["jsonld_company"])
    res = RESOLVED.get(entry["file"], {})
    if "title" in res:
        title, method = res["title"], "resolved by reading the page: " + res["why"]
        notes.append(f"title: {res['why']}")
    if "company" in res:
        company, company_how = res["company"], "resolved by reading the page: " + res["why"]
        notes.append(f"company: {res['why']}")
    unresolved = []
    if method is None:
        unresolved.append(f"title: listing {api_title!r} vs JSON-LD {src['jsonld_title']!r} / h1 {src['h1']} / "
                          f"h2 {src['h2']}")
    if not company or (not checks["company_in_page"] and "company" not in res):
        unresolved.append(f"company: {company!r} ({company_how}); og:site_name {src['og_site_name']!r}; "
                          f"document title {src['document_title']!r}")
    return {"title": title, "company": company, "method": {"title": method, "company": company_how},
            "checks": checks, "sources": src, "notes": notes, "unresolved": unresolved}


def cmd_key() -> None:
    log = json.loads((OUT / "fetch_log.json").read_text())
    key, problems = [], []
    for e in log["pages"]:
        d = decide(e, load_html(e["file"]))
        row = {"file": e["file"], "platform": e["platform"], "board": e["board"],
               "site": site_of(e["final_url"]), "url": e["final_url"], "requested_url": e["url"],
               "fetched_at": e["fetched_at"], "document_title": e["document_title"],
               "title": d["title"], "company": d["company"], "accept": {}, "checked_by": CHECKED_BY,
               "method": d["method"], "checks": d["checks"], "notes": d["notes"]}
        if d["unresolved"]:
            problems.append({"file": e["file"], "unresolved": d["unresolved"], "sources": d["sources"]})
            row["notes"] = row["notes"] + ["UNRESOLVED: " + u for u in d["unresolved"]]
        key.append(row)
    KEY.write_text(json.dumps(key, indent=2, ensure_ascii=False) + "\n")
    per = {}
    for r in key:
        per[r["platform"]] = per.get(r["platform"], 0) + 1
    print(f"{len(key)} entries -> {KEY.relative_to(REPO)}; per platform {per}; unresolved {len(problems)}")
    for p in problems:
        print(json.dumps(p, ensure_ascii=False))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "list":
        cmd_list()
    elif cmd == "render":
        cmd_render(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "key":
        cmd_key()
    else:
        raise SystemExit(__doc__)
