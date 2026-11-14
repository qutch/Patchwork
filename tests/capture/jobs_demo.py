"""The page -> form demonstration (V11), driven headless through the real recorder.

A person on `jobs.lever.co` copies the job title and the company, then fills the tracker form: pastes both, types
an email, the link and today's date, and clicks Submit. The Lever page is `fixtures/pages/lever-1.html` served at
its real URL by a Playwright route (no network: everything else is aborted). The form is the fixture tracker form
(`tests/capture/form_app.py`) on a free port, reached under the stable alias `FORM_ALIAS` so recordings do not
depend on the port. Used by tests/capture, tests/matcher and scripts/make_recording_jobs.py.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from capture.launcher import finalize
from capture.recorder import Recorder, now_ms

REPO = Path(__file__).resolve().parents[2]
PAGES = REPO / "fixtures" / "pages"
LEVER_URL = json.loads((PAGES / "index.json").read_text())[0]["url"]
LEVER_FILE = PAGES / "lever-1.html"
FORM_ALIAS = "http://127.0.0.1:18010"   # V0's dev port for the fixture form; never bound here, only routed
EMAIL = "demo@example.com"
INTENT = "Add the job posting I'm looking at to my application tracker"

SELECT_AND_COPY = """([sel, text]) => {
  const root = document.querySelector(sel);
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    const i = node.textContent.indexOf(text);
    if (i < 0) continue;
    const r = document.createRange(); r.setStart(node, i); r.setEnd(node, i + text.length);
    const s = getSelection(); s.removeAllRanges(); s.addRange(r);
    document.execCommand('copy');
    return s.toString();
  }
  throw new Error('text not found: ' + text);
}"""

PASTE = """(el, text) => {
  el.focus();
  const dt = new DataTransfer(); dt.setData('text/plain', text);
  el.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
}"""


async def route_pages(context, form_base: str) -> None:
    async def handle(route):
        url = route.request.url
        if url.split("#")[0].split("?")[0] == LEVER_URL:
            await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=LEVER_FILE.read_bytes())
        elif url.startswith(FORM_ALIAS):
            resp = await route.fetch(url=form_base + url[len(FORM_ALIAS):])  # redirects followed server side
            await route.fulfill(response=resp)
        else:
            await route.abort()
    await context.route("**/*", handle)


async def demonstrate(page, *, copy_company: bool = True, today: str | None = None) -> dict:
    """The person's steps. Returns what they sent."""
    sent = {"Email": EMAIL, "Job Title": "American Tech Fellowship", "Company": "Palantir Technologies",
            "Link": LEVER_URL, "Date Applied": today or date.today().isoformat()}
    await page.goto(LEVER_URL)
    title = await page.evaluate(SELECT_AND_COPY, [".posting-headline", sent["Job Title"]])
    company = await page.evaluate(SELECT_AND_COPY, [".main-footer", sent["Company"]]) if copy_company else None

    await page.goto(FORM_ALIAS + "/")
    await page.get_by_role("textbox", name="Your email").fill(EMAIL)
    for label, text in (("Job Title", title), ("Company", company)):
        if text is None:
            await page.get_by_role("textbox", name=label, exact=True).fill(sent[label])
            continue
        box = page.get_by_role("textbox", name=label, exact=True)
        await box.evaluate(PASTE, text)
        await box.fill(text)
    await page.get_by_role("textbox", name="Link", exact=True).fill(sent["Link"])
    await page.get_by_role("textbox", name="Date Applied", exact=True).fill(sent["Date Applied"])
    await page.get_by_role("button", name="Submit", exact=True).click()
    await page.get_by_text("Your response has been recorded.").wait_for(timeout=10000)
    await page.wait_for_timeout(300)  # let the confirmation event land
    return sent


async def record_jobs_demo(out: Path, form_base: str, *, allowlist: list[str] | None = None,
                           recording_id: str | None = None, **kw) -> tuple[dict, dict]:
    """Record the demonstration into `out`; return (finalized recording, values sent)."""
    from playwright.async_api import async_playwright

    allowlist = allowlist or ["jobs.lever.co", FORM_ALIAS.split("//", 1)[1]]
    meta = {"id": recording_id or out.name, "intent": INTENT, "allowlist": allowlist, "started_at": now_ms(),
            "start_url": LEVER_URL}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        await route_pages(context, form_base)
        rec = Recorder(out, allowlist)
        await rec.attach(context)
        page = await context.new_page()
        sent = await demonstrate(page, **kw)
        await rec.flush()
        rec.close()
        await browser.close()
    return finalize(out, meta), sent
