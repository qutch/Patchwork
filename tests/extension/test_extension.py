"""The Chrome MV3 extension (docs/contracts.md V11) against a stub control plane implementing V9's
POST /api/pages, GET /api/pages/{id} and GET /api/extension. Headless Chromium (the full `chromium` channel:
the default headless shell cannot load extensions) with the unpacked extension loaded."""

from __future__ import annotations

import re

import threading
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from playwright.sync_api import expect
from fastapi.responses import HTMLResponse, JSONResponse

from tests.conftest import _Server

REPO = Path(__file__).resolve().parents[2]
EXT = REPO / "extension"
TOKEN = "tok-test-123"
JOB_PAGE = """<!doctype html><html><head><title>Senior Widget Engineer - Acme</title></head>
<body><div class="posting"><h2>Senior Widget Engineer</h2><p>Acme Corp</p></div>
<script>document.body.insertAdjacentHTML('beforeend', '<p id="rendered">rendered by js</p>')</script></body></html>"""


class Stub:
    """V9 stub: records posted pages; the row for a page goes through `script` (a list of TrackerRow updates)."""

    def __init__(self):
        self.posts: list[dict] = []
        self.script: list[dict] = [{"status": "success", "label": "code"}]
        self.gets: dict[str, int] = {}
        self.integrations: list[dict] | None = None  # V15: None = a control plane from before V15
        self.lock = threading.Lock()
        app = FastAPI()

        @app.post("/api/pages")
        async def post_page(request: Request):
            body = await request.json()
            with self.lock:
                self.posts.append({"body": body, "token": request.headers.get("x-patchwork-token")})
                if request.headers.get("x-patchwork-token") != TOKEN:
                    return JSONResponse({"detail": "bad token"}, status_code=401)
                seq = len([p for p in self.posts if p["token"] == TOKEN])
            pid = f"pg-{seq:06d}"
            return JSONResponse({"id": pid, "seq": seq, "site": "127.0.0.1", "canonical_url": body["url"],
                                 "integration": "jobs_to_tracker", "run_id": 1, "queued": False}, status_code=201)

        @app.get("/api/pages/{pid}")
        def get_page(pid: str):
            with self.lock:
                n = self.gets.get(pid, 0)
                self.gets[pid] = n + 1
            step = self.script[min(n, len(self.script) - 1)]
            row = {"page_id": pid, "title": "Senior Widget Engineer", "company": "Acme Corp", "status": "pending",
                   "label": None, "reason": None, **step}
            return {"page": {"id": pid}, "row": row}

        @app.get("/api/extension")
        def extension():
            out = {"token": TOKEN, "control_url": self.url}
            if self.integrations is not None:
                out.update(integrations=self.integrations,
                           default_integration=self.integrations[0]["name"] if self.integrations else None)
            return out

        @app.get("/job", response_class=HTMLResponse)
        def job():
            return JOB_PAGE

        self.app = app


@pytest.fixture(scope="module")
def stub():
    s = Stub()
    with _Server(s.app) as server:
        s.url = server.url
        yield s


@pytest.fixture(scope="module")
def browser_ext(tmp_path_factory):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(tmp_path_factory.mktemp("chrome-profile")), channel="chromium", headless=True,
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}"])
        sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=10000)
        yield ctx, sw
        ctx.close()


@pytest.fixture
def ext(browser_ext, stub):
    ctx, sw = browser_ext
    sw.evaluate("chrome.storage.local.remove('integration')")
    sw.evaluate("o => chrome.storage.local.set(o)", {"controlUrl": stub.url, "token": TOKEN})
    with stub.lock:
        stub.integrations = None
        stub.posts.clear()
        stub.gets.clear()
        stub.script = [{"status": "success", "label": "code"}]
    for p in ctx.pages[1:]:
        p.close()
    return ctx, sw


def _open_job(ctx, stub):
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(stub.url + "/job")
    page.bring_to_front()
    return page


BADGE_OF_ACTIVE_TAB = """async () => {
  const [t] = await chrome.tabs.query({active: true, lastFocusedWindow: true});
  return {text: await chrome.action.getBadgeText({tabId: t.id}), title: await chrome.action.getTitle({tabId: t.id})};
}"""


def test_send_page_posts_dom_and_token(ext, stub):
    ctx, sw = ext
    _open_job(ctx, stub)
    result = sw.evaluate("sendActiveTab()")
    assert result["ok"] and result["id"] == "pg-000001" and result["state"] == "code"
    assert len(stub.posts) == 1
    post = stub.posts[0]
    assert post["token"] == TOKEN
    assert post["body"]["url"] == stub.url + "/job"
    assert post["body"]["title"] == "Senior Widget Engineer - Acme"
    assert "<h2>Senior Widget Engineer</h2>" in post["body"]["html"]
    assert 'id="rendered"' in post["body"]["html"]  # the rendered DOM, not the served source
    assert post["body"]["html"].startswith("<html")


@pytest.mark.parametrize("script,badge", [
    ([{"status": "success", "label": "code"}], "✓"),
    ([{"status": "running"}, {"status": "learning"}, {"status": "success", "label": "learned_now"}], "AI"),
    ([{"status": "held", "label": "held", "reason": "PARSE_NO_MATCH"}], "!"),
    ([{"status": "duplicate", "label": "duplicate"}], "dup"),
    ([{"status": "failed"}], "!"),
])
def test_badge_reflects_outcome(ext, stub, script, badge):
    ctx, sw = ext
    stub.script = script
    _open_job(ctx, stub)
    result = sw.evaluate("sendActiveTab()")
    assert result["ok"]
    got = sw.evaluate(BADGE_OF_ACTIVE_TAB)
    assert got["text"] == badge
    assert stub.gets["pg-000001"] == len(script)
    if badge == "!" and script[0].get("reason"):
        assert "PARSE_NO_MATCH" in got["title"]


def test_bad_token_shows_error_and_writes_nothing(ext, stub):
    ctx, sw = ext
    sw.evaluate("chrome.storage.local.set({token: 'wrong'})")
    _open_job(ctx, stub)
    result = sw.evaluate("sendActiveTab()")
    assert not result["ok"] and "token" in result["error"]
    assert sw.evaluate(BADGE_OF_ACTIVE_TAB)["text"] == "!"
    assert stub.gets == {}


def test_unreachable_control_plane_is_reported(ext, stub):
    ctx, sw = ext
    sw.evaluate("chrome.storage.local.set({controlUrl: 'http://127.0.0.1:9'})")
    _open_job(ctx, stub)
    result = sw.evaluate("sendActiveTab()")
    assert not result["ok"] and "not reachable" in result["error"]


def test_nothing_is_sent_without_a_press(ext, stub):
    ctx, _ = ext
    page = _open_job(ctx, stub)
    page.reload()
    page.wait_for_timeout(500)
    assert stub.posts == []


def test_options_persist(ext, stub):
    ctx, sw = ext
    sw.evaluate("chrome.storage.local.clear()")
    ext_id = sw.url.split("/")[2]
    page = ctx.new_page()
    page.goto(f"chrome-extension://{ext_id}/options.html")
    # options.js fills the inputs after an async chrome.storage read. Wait with expect(): the extension
    # page's CSP forbids eval, so a polling wait_for_function(<string>) fails whenever it has to poll.
    expect(page.locator("#controlUrl")).to_have_value("http://127.0.0.1:8000")  # the default
    page.fill("#controlUrl", "http://127.0.0.1:38080/")
    page.fill("#token", "  tok-typed  ")
    page.click("#save")
    expect(page.locator("#status")).to_have_text(re.compile(r"^Saved"))
    assert sw.evaluate("chrome.storage.local.get(null)") == {"controlUrl": "http://127.0.0.1:38080",
                                                             "token": "tok-typed"}
    page.reload()
    expect(page.locator("#token")).to_have_value("tok-typed")

    # "Get token from Patchwork" reads GET /api/extension at the address typed.
    page.fill("#controlUrl", stub.url)
    page.click("#fetch")
    expect(page.locator("#status")).to_have_text(re.compile(r"^Connected"))
    assert sw.evaluate("chrome.storage.local.get(null)") == {"controlUrl": stub.url, "token": TOKEN}
    page.close()


def test_manifest_is_minimal():
    import json
    m = json.loads((EXT / "manifest.json").read_text())
    assert m["manifest_version"] == 3
    assert sorted(m["permissions"]) == ["activeTab", "scripting", "storage"]
    assert m["host_permissions"] == ["http://127.0.0.1/*", "http://localhost/*"]
    assert "content_scripts" not in m
    assert m["action"]["default_popup"] == "popup.html"  # V15: the popup switches the integration
    assert (EXT / "popup.html").exists() and (EXT / "integrations.js").exists()
    assert m["commands"]["send-page"]["suggested_key"]["default"] == "Alt+Shift+P"
    for size in ("16", "32", "48", "128"):
        assert (EXT / m["icons"][size]).exists()


# ---- V15: which integration a page goes to ----------------------------------------------------------

INTEGRATIONS = [{"name": "jobs_to_tracker", "intent": "Add every job posting I send to my tracker"},
                {"name": "track_product_prices", "intent": "Track product prices"}]


def test_untagged_by_default(ext, stub):
    ctx, sw = ext
    _open_job(ctx, stub)
    assert sw.evaluate("sendActiveTab()")["ok"]
    assert "integration" not in stub.posts[0]["body"]  # Patchwork's default integration decides


def test_options_dropdown_lists_integrations_and_persists_the_choice(ext, stub):
    ctx, sw = ext
    stub.integrations = INTEGRATIONS
    ext_id = sw.url.split("/")[2]
    page = ctx.new_page()
    page.goto(f"chrome-extension://{ext_id}/options.html")
    sel = page.locator("#integration")
    expect(sel.locator("option")).to_have_count(3)
    expect(sel.locator("option").first).to_have_text("Patchwork's default (jobs_to_tracker)")
    expect(sel.locator("option").nth(2)).to_have_text("track_product_prices: Track product prices")
    sel.select_option("track_product_prices")
    page.click("#save")
    expect(page.locator("#status")).to_have_text(re.compile(r"^Saved"))
    assert sw.evaluate("chrome.storage.local.get(null)")["integration"] == "track_product_prices"
    page.reload()
    expect(page.locator("#integration")).to_have_value("track_product_prices")
    page.locator("#integration").select_option("")  # back to the default: the key is removed
    page.click("#save")
    expect(page.locator("#status")).to_have_text(re.compile(r"^Saved"))
    assert "integration" not in sw.evaluate("chrome.storage.local.get(null)")
    page.close()


def test_popup_switches_the_integration_and_pages_carry_it(ext, stub):
    ctx, sw = ext
    stub.integrations = INTEGRATIONS
    ext_id = sw.url.split("/")[2]
    popup = ctx.new_page()
    popup.goto(f"chrome-extension://{ext_id}/popup.html")
    expect(popup.locator("#integration option")).to_have_count(3)
    popup.locator("#integration").select_option("track_product_prices")
    expect(popup.locator("#status")).to_have_text(re.compile("track_product_prices"))
    assert sw.evaluate("chrome.storage.local.get(null)")["integration"] == "track_product_prices"
    popup.close()
    _open_job(ctx, stub)
    result = sw.evaluate("sendActiveTab()")
    assert result["ok"] and stub.posts[-1]["body"]["integration"] == "track_product_prices"


def test_popup_without_token_points_to_options(ext, stub):
    ctx, sw = ext
    sw.evaluate("chrome.storage.local.remove('token')")
    ext_id = sw.url.split("/")[2]
    popup = ctx.new_page()
    popup.goto(f"chrome-extension://{ext_id}/popup.html")
    expect(popup.locator("#status")).to_have_text(re.compile("token"))
    popup.close()
