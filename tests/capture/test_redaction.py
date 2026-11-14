"""Redaction is on by default: secret fields, secret headers and non-allowlisted hosts never
reach events.jsonl."""

from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from capture.recorder import filter_headers, host_allowed, parse_body, redact_json, MAX_BODY
from tests.capture.helpers import netloc, record_with, run
from tests.conftest import _Server

SECRETS = ["hunter2", "4111111111111111", "424242", "s3cr3t-token", "Bearer abc.def"]

LOGIN = """<!doctype html><html><head><title>Login</title></head><body>
<form id="login">
  <label>User <input name="user" data-testid="user"></label>
  <label>Password <input name="password" type="password" data-testid="pw"></label>
  <label>Card <input name="card" autocomplete="cc-number" data-testid="card"></label>
  <label>Code <input name="otp" autocomplete="one-time-code" data-testid="otp"></label>
  <button type="submit">Sign in</button>
</form><p id="status"></p>
<script>
document.getElementById('login').addEventListener('submit', async (e) => {
  e.preventDefault();
  const body = Object.fromEntries(new FormData(e.target));
  body.api_token = 's3cr3t-token';
  const r = await fetch('/api/login', {method: 'POST', body: JSON.stringify(body),
    headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer abc.def', 'X-Client': 'demo'}});
  try { await fetch(window.OTHER + '/api/messages'); } catch (err) {}
  document.getElementById('status').textContent = r.ok ? 'ok' : 'err';
});
</script></body></html>"""


def _login_app() -> FastAPI:
    app = FastAPI()

    @app.get("/", response_class=HTMLResponse)
    def page() -> HTMLResponse:
        resp = HTMLResponse(LOGIN)
        resp.set_cookie("session", "cookie-secret-value")
        return resp

    @app.post("/api/login")
    async def login(request: Request) -> JSONResponse:
        data = await request.json()
        return JSONResponse({"ok": True, "user": data.get("user"), "token": "s3cr3t-token"})

    return app


def test_browser_redaction(apps, workspace):
    out = workspace / "recordings" / "rec-redact"
    with _Server(_login_app()) as site:
        async def script(page):
            await page.goto(site.url + "/")
            await page.evaluate(f"window.OTHER = {json.dumps(apps['inbox'])}")
            await page.fill("[data-testid=user]", "alice")
            await page.fill("[data-testid=pw]", "hunter2")
            await page.press("[data-testid=pw]", "Tab")
            await page.fill("[data-testid=card]", "4111111111111111")
            await page.fill("[data-testid=otp]", "424242")
            await page.click("[data-testid=pw]")
            await page.click("button[type=submit]")
            await page.wait_for_function("document.getElementById('status').textContent !== ''")
            await page.wait_for_timeout(300)
            # A page on a host that is not on the allowlist: nothing from it is recorded.
            await page.goto(apps["inbox"] + "/")
            await page.wait_for_timeout(300)

        recording = run(record_with(out, [netloc(site.url)], script))

    raw = (out / "events.jsonl").read_text()
    for secret in SECRETS + ["cookie-secret-value"]:
        assert secret not in raw, f"{secret!r} leaked into the recording"
    assert apps["inbox"] not in raw and netloc(apps["inbox"]) not in raw

    events = recording["events"]
    fields = {e["el"]["name"] for e in events if e["type"] in ("input", "change", "paste", "click", "keydown")}
    assert "user" in fields and not fields & {"password", "card", "otp"}
    assert [e["value"] for e in events if e["type"] == "input"] == ["alice"]

    post = next(e for e in events if e["type"] == "net" and e["method"] == "POST")
    # password/api_token by key pattern; card/otp because the page marked those fields sensitive
    assert post["req"] == {"user": "alice", "password": "[REDACTED]", "card": "[REDACTED]",
                           "otp": "[REDACTED]", "api_token": "[REDACTED]"}
    assert post["resp"]["token"] == "[REDACTED]" and post["resp"]["user"] == "alice"
    headers = {k.lower() for k in post.get("req_headers", {})}
    assert "x-client" in headers and not headers & {"authorization", "cookie"}
    assert all(e["type"] != "framenavigated" or e["url"].startswith(site.url) for e in events)


def test_allowlist_matching():
    allow = ["localhost:8001", "example.com"]
    assert host_allowed("http://localhost:8001/api/messages", allow)
    assert not host_allowed("http://localhost:8002/api/incoming", allow)
    assert host_allowed("https://example.com:8443/x", allow)  # bare host: any port
    assert host_allowed("https://example.com/x", allow)
    assert not host_allowed("https://evil.example.com/x", allow)
    assert not host_allowed("about:blank", allow)
    assert host_allowed("http://localhost/", ["localhost:80"])


def test_header_and_body_redaction():
    kept = filter_headers({"Authorization": "Bearer x", "Cookie": "a=b", "content-type": "application/json",
                           "X-Api-Key": "k", "X-Request-Id": "r1", "user-agent": "ua", "sec-ch-ua": "x"})
    assert kept == {"content-type": "application/json", "X-Request-Id": "r1"}
    assert redact_json({"user": "a", "Password": "p", "nested": [{"card_number": "4", "qty": 2}], "token": ""}) == \
        {"user": "a", "Password": "[REDACTED]", "nested": [{"card_number": "[REDACTED]", "qty": 2}], "token": ""}
    assert parse_body("user=a&password=b", "application/x-www-form-urlencoded") == \
        ({"user": "a", "password": "[REDACTED]"}, False)
    big, truncated = parse_body(json.dumps(["x" * 100] * 400), "application/json")
    assert truncated and isinstance(big, str) and len(big.encode()) <= MAX_BODY
    assert parse_body(None) == (None, False)
