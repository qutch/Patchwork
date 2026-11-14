"""Spike A: label-keyed Google Form fill-and-submit with headless Playwright.

Library part: snapshot_form(page), resolve_control(page, label), fill_and_submit(page, values, ...).
Runner part (python spike_a.py dry|run): loads the demo form, snapshots it, and in `run` mode
submits exactly 5 times (3 warm-browser, 2 cold-browser), then checks the linked Sheet's CSV.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import Locator, Page, sync_playwright

FORM_URL = "https://docs.google.com/forms/d/e/1FAIpQLSeJSRbMx1pr9nMwj4n1E5vOUxmz2Zo19-lJXyz0-8HhmfeNoQ/viewform"
SHEET_ID = "1sEDvJMO4sQN3RJxo1JKFqqfrDfQTjZrNL2Ztlz0j60M"
SHEET_GID = "2029966864"  # "Form Responses 1" (not 0; gid=0 returns 400)
CONFIRMATION_TEXT = "Your response has been recorded."
REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "fixtures" / "google_form"

# Controls a question can hold. Order matters: first match wins for type detection.
_CONTROL_SELECTORS = [
    ("radio", "[role=radio]"),
    ("checkbox", "[role=checkbox]"),
    ("dropdown", "[role=listbox]"),
    ("date", "input[type=date]"),
    ("time", "input[type=time]"),
    ("paragraph", "textarea"),
    ("email", "input[type=email]"),
    ("text", "input[type=text], input:not([type]), input[type=url], input[type=number]"),
]


class FormError(RuntimeError):
    pass


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().rstrip("*").strip().casefold()


def _question_items(page: Page) -> list[Locator]:
    # Each question is a role=listitem containing a role=heading (the question title).
    return [li for li in page.locator("[role=listitem]").all() if li.locator("[role=heading]").count()]


def _item_label(li: Locator) -> str:
    h = li.locator("[role=heading]").first
    # Title text minus the " *" required marker span (aria-label="Required question").
    return h.evaluate(
        """h => { const c = h.cloneNode(true);
                 c.querySelectorAll('[aria-label="Required question"]').forEach(e => e.remove());
                 return c.textContent.replace(/\\s+/g,' ').trim(); }"""
    )


def _item_kind(li: Locator) -> tuple[str, Locator]:
    for kind, sel in _CONTROL_SELECTORS:
        loc = li.locator(sel)
        if loc.count():
            return kind, loc
    return "unknown", li.locator("input, textarea")


def snapshot_form(page: Page) -> list[dict]:
    out = []
    for li in _question_items(page):
        kind, ctl = _item_kind(li)
        required = bool(
            li.locator('[aria-label="Required question"]').count()
            or li.locator("[required], [aria-required=true]").count()
        )
        options: list[str] = []
        if kind in ("radio", "checkbox"):
            options = [c.get_attribute("aria-label") or c.get_attribute("data-value") or c.inner_text().strip() for c in ctl.all()]
        elif kind == "dropdown":
            options = [o.get_attribute("data-value") or o.inner_text().strip() for o in li.locator("[role=option]").all()]
            options = [o for o in options if o and o != "Choose"]
        out.append({"label": _item_label(li), "input": kind, "required": required, "options": options})
    return out


def snapshot_hash(snap: list[dict]) -> str:
    canon = sorted(
        (q["label"], q["input"], q["required"], tuple(sorted(q["options"]))) for q in snap
    )
    return hashlib.sha256(json.dumps(canon).encode()).hexdigest()[:16]


def resolve_control(page: Page, label: str) -> tuple[str, Locator, str]:
    """Return (kind, locator, via). Raises FormError unless exactly one question matches."""
    # 1) accessible-name match (aria-labelledby -> heading). Exact, case-sensitive.
    loc = page.get_by_label(label, exact=True)
    n = loc.count()
    if n == 1:
        kind = loc.evaluate("e => e.tagName === 'TEXTAREA' ? 'paragraph' : (e.getAttribute('role') || e.type || 'text')")
        return kind, loc, "get_by_label(exact)"
    if n > 1:
        raise FormError(f"label {label!r} resolves to {n} controls via get_by_label")
    # 2) question heading match (normalized: casefold, whitespace, trailing '*').
    hits = [li for li in _question_items(page) if _norm(_item_label(li)) == _norm(label)]
    if len(hits) != 1:
        raise FormError(f"label {label!r} matches {len(hits)} questions (known: {[q['label'] for q in snapshot_form(page)]})")
    kind, ctl = _item_kind(hits[0])
    if kind in ("radio", "checkbox"):
        return kind, hits[0], "heading(group)"
    if ctl.count() != 1:
        raise FormError(f"question {label!r} holds {ctl.count()} {kind} controls")
    return kind, ctl, "heading"


def _fill_one(page: Page, kind: str, loc: Locator, value: str) -> None:
    if kind == "radio":
        loc.get_by_role("radio", name=value, exact=True).click()
    elif kind == "checkbox":
        for v in [v.strip() for v in value.split(",") if v.strip()]:
            loc.get_by_role("checkbox", name=v, exact=True).click()
    elif kind in ("dropdown", "listbox"):
        loc.click()
        page.get_by_role("option", name=value, exact=True).last.click()
    else:  # text/email/date(YYYY-MM-DD)/time(HH:MM)/paragraph
        loc.fill(value)
        if loc.input_value() != value:
            raise FormError(f"fill did not stick: wanted {value!r}, got {loc.input_value()!r}")


def fill_and_submit(page: Page, values: dict[str, str], submit: str = "Submit",
                    confirmation_text: str = CONFIRMATION_TEXT, timeout_ms: int = 15000) -> dict:
    t0 = time.perf_counter()
    resolved = {label: resolve_control(page, label) for label in values}  # resolve all before typing
    for label, (kind, loc, _via) in resolved.items():
        _fill_one(page, kind, loc, values[label])
    t1 = time.perf_counter()
    btn = page.get_by_role("button", name=submit, exact=True)
    if btn.count() != 1:
        raise FormError(f"submit button {submit!r} resolves to {btn.count()}")
    btn.click()
    try:
        page.get_by_text(confirmation_text, exact=False).first.wait_for(timeout=timeout_ms)
    except Exception as e:
        alerts = [a.strip() for a in page.locator("[role=alert]").all_inner_texts() if a.strip()]
        raise FormError(f"no confirmation; url={page.url} alerts={alerts}") from e
    t2 = time.perf_counter()
    return {"fill_s": t1 - t0, "submit_s": t2 - t1, "url": page.url,
            "via": {k: v[2] for k, v in resolved.items()}}


# ---------------------------------------------------------------- runner

def fetch_csv(kind: str) -> tuple[int, list[list[str]]]:
    url = {
        "export": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SHEET_GID}",
        "gviz": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv&sheet=Form%20Responses%201",
    }[kind]
    r = httpx.get(url, follow_redirects=True, timeout=20, headers={"Cache-Control": "no-cache"})
    return r.status_code, list(csv.reader(io.StringIO(r.text))) if r.status_code == 200 else []


def wait_row(job_title: str, kind: str, limit_s: float = 60) -> float | None:
    t = time.perf_counter()
    while time.perf_counter() - t < limit_s:
        _, rows = fetch_csv(kind)
        if any(job_title in row for row in rows):
            return time.perf_counter() - t
        time.sleep(0.5)
    return None


def values_for(n: int) -> dict[str, str]:
    return {
        "Email": "spike@example.com",
        "Job Title": f"SPIKE test {n}",
        "Company": "Spike Co",
        "Link": f"https://example.com/spike-{n}",
        "Date Applied": time.strftime("%Y-%m-%d"),
    }


def load(page: Page) -> float:
    t = time.perf_counter()
    page.goto(FORM_URL, wait_until="domcontentloaded")
    page.get_by_role("button", name="Submit", exact=True).wait_for()
    return time.perf_counter() - t


def main(mode: str) -> None:
    results, hashes = [], []
    max_attempts = 7  # 5 submissions + at most 2 retries, never more
    attempts = 0
    with sync_playwright() as p:
        # --- warm: one browser, fresh context per submission
        t = time.perf_counter(); browser = p.chromium.launch(headless=True); launch = time.perf_counter() - t
        for i in range(2):  # snapshot stability on two separate loads
            ctx = browser.new_context(); pg = ctx.new_page(); load(pg)
            snap = snapshot_form(pg); hashes.append(snapshot_hash(snap))
            if i == 0:
                print(json.dumps(snap, indent=1))
                for label in values_for(0):
                    kind, loc, via = resolve_control(pg, label); print(f"resolve {label!r}: {kind} via {via} count={loc.count()}")
                if mode == "run":
                    FIXTURES.mkdir(parents=True, exist_ok=True)
                    (FIXTURES / "viewform.html").write_text(pg.content())
            ctx.close()
        print("snapshot hashes", hashes, "stable" if len(set(hashes)) == 1 else "UNSTABLE")
        if mode != "run":
            browser.close(); return

        plan = [("warm", 1), ("warm", 2), ("warm", 3), ("cold", 4), ("cold", 5)]
        for mode_i, n in plan:
            while True:
                attempts += 1
                if attempts > max_attempts:
                    raise SystemExit("retry budget exhausted")
                rec = {"n": n, "mode": mode_i}; submitted = False
                try:
                    if mode_i == "cold":
                        t = time.perf_counter(); b = p.chromium.launch(headless=True); rec["launch_s"] = time.perf_counter() - t
                    else:
                        b = browser; rec["launch_s"] = launch if n == 1 else 0.0
                    ctx = b.new_context(); pg = ctx.new_page()
                    rec["load_s"] = load(pg)
                    r = fill_and_submit(pg, values_for(n))
                    rec.update(r); submitted = True
                    if n == 1:
                        (FIXTURES / "confirmation.html").write_text(pg.content())
                        body = pg.inner_text("body"); print("CONFIRMATION BODY:\n", body[:600])
                        print("submit-another link:", pg.get_by_role("link").all_inner_texts())
                    ctx.close()
                    if mode_i == "cold":
                        b.close()
                    t_sub = time.perf_counter()
                    rec["gviz_lag_s"] = wait_row(f"SPIKE test {n}", "gviz")
                    rec["export_lag_s"] = (time.perf_counter() - t_sub) if wait_row(f"SPIKE test {n}", "export") is not None else None
                    results.append(rec); print(json.dumps(rec)); break
                except Exception as e:
                    print("ATTEMPT FAILED", n, repr(e)); rec["error"] = repr(e)
                    if submitted:  # confirmed already; never resubmit
                        results.append(rec); break
        browser.close()

    for kind in ("export", "gviz"):
        status, rows = fetch_csv(kind)
        spikes = [r for r in rows if any(c.startswith("SPIKE test") for c in r)]
        print(kind, status, "header:", rows[0] if rows else None, "spike rows:", len(spikes))
        for r in spikes:
            print("  ", r)
    print("attempts used:", attempts)
    json.dump({"results": results, "hashes": hashes}, open(Path(__file__).with_name("spike_a_results.json"), "w"), indent=1)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "dry")
