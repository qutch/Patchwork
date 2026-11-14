"""Python side of the recorder: attaches recorder.js and network/navigation hooks to a
Playwright BrowserContext and appends every captured event to `<dir>/events.jsonl`.

Redaction is always on:
  - DOM: recorder.js never emits values from password / cc-* / one-time-code fields.
  - Network: only XHR/fetch to allowlisted hosts, same-origin with the page; Authorization,
    Cookie (and similar) headers are dropped; values under secret-looking JSON keys are masked.
  - Navigation and DOM events from non-allowlisted hosts are dropped.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

RECORDER_JS = Path(__file__).with_name("recorder.js")
MAX_BODY = 20 * 1024
DOM_TYPES = {"click", "input", "change", "submit", "keydown", "copy", "paste"}
SECRET_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key", "x-auth-token",
                  "x-csrf-token", "x-xsrf-token"}
NOISY_HEADERS = {"user-agent", "accept", "accept-encoding", "accept-language", "referer", "origin", "connection",
                 "host", "content-length", "cache-control", "pragma", "priority"}
SECRET_KEY = re.compile(r"(passw|secret|token|api[_-]?key|otp|cvv|cvc|card[_-]?num|authoriz)", re.I)
REDACTED = "[REDACTED]"


def now_ms() -> int:
    return int(time.time() * 1000)


def netloc(url: str) -> str:
    p = urlsplit(url)
    if not p.hostname:
        return ""
    port = p.port or {"http": 80, "https": 443}.get(p.scheme)
    return f"{p.hostname}:{port}"


def host_allowed(url: str, allowlist: list[str]) -> bool:
    """`host:port` entries match exactly; a bare `host` matches any port."""
    loc = netloc(url)
    if not loc:
        return False
    host = loc.rsplit(":", 1)[0]
    return any(a == loc or (":" not in a and a == host) for a in allowlist)


def origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def redact_json(value: Any, keys: frozenset[str] | set[str] = frozenset()) -> Any:
    """Mask values under secret-looking keys, or keys of sensitive form fields seen on the page."""
    if isinstance(value, dict):
        return {k: (REDACTED if (SECRET_KEY.search(str(k)) or k in keys) and v not in (None, "")
                    else redact_json(v, keys))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact_json(v, keys) for v in value]
    return value


def filter_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items()
            if k.lower() not in SECRET_HEADERS and k.lower() not in NOISY_HEADERS
            and not k.lower().startswith(("sec-", ":"))}


def parse_body(text: str | None, content_type: str = "",
               keys: frozenset[str] | set[str] = frozenset()) -> tuple[Any, bool]:
    """-> (body, truncated). JSON is parsed, forms become dicts, other text is kept as a string."""
    if text is None or text == "":
        return None, False
    if len(text.encode()) > MAX_BODY:
        return text.encode()[:MAX_BODY].decode(errors="ignore"), True
    try:
        return redact_json(json.loads(text), keys), False
    except ValueError:
        pass
    if "application/x-www-form-urlencoded" in content_type:
        return redact_json(dict(parse_qsl(text, keep_blank_values=True)), keys), False
    return text, False


class EventLog:
    """Append-only events.jsonl; one JSON object per line, flushed per event."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")
        self.count = 0

    def write(self, event: dict) -> None:
        if self._fh.closed:
            return
        self._fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._fh.flush()
        self.count += 1

    def close(self) -> None:
        self._fh.close()


def read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue  # a half-written last line
    return out


class Recorder:
    """Attach to a fresh BrowserContext *before* opening pages, then drive or hand it to a human."""

    def __init__(self, out_dir: Path, allowlist: list[str]):
        self.dir = Path(out_dir)
        self.allowlist = list(allowlist)
        self.log = EventLog(self.dir / "events.jsonl")
        self._req_t: dict[Any, int] = {}
        self._tasks: set[asyncio.Task] = set()
        self._navs: set[asyncio.Task] = set()  # framenavigated writes still waiting for the title
        self.context = None
        self.sensitive_keys: set[str] = set()  # names of password/cc/otp fields seen on pages

    # ---- wiring ----
    async def attach(self, context) -> None:
        self.context = context
        await context.expose_binding("pwRecord", self._on_dom)
        await context.add_init_script(path=str(RECORDER_JS))
        context.on("page", self._on_page)
        context.on("request", self._on_request)
        context.on("requestfinished", lambda r: self._spawn(self._on_finished(r)))
        context.on("requestfailed", lambda r: self._spawn(self._on_failed(r)))
        for p in context.pages:
            self._on_page(p)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _after_navs(self) -> None:
        """A navigation's event is written once its title is known (after domcontentloaded),
        so the page's own first fetches can finish earlier. Wait for navigations already in
        flight so the live log stays in the order things happened."""
        pending = [n for n in self._navs if not n.done()]
        if pending:
            await asyncio.wait(pending)

    def _on_page(self, page) -> None:
        page.on("framenavigated", lambda frame: self._on_nav(page, frame))

    # ---- DOM ----
    async def _on_dom(self, source: dict, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        if payload.get("type") == "sensitive_field" and isinstance(payload.get("name"), str):
            self.sensitive_keys.add(payload["name"])
            return
        if payload.get("type") not in DOM_TYPES:
            return
        url = payload.get("url") or ""
        if not host_allowed(url, self.allowlist):
            return
        event = {"t": int(payload.get("t") or now_ms()), "type": payload["type"], "url": url}
        for k in ("key", "value", "text"):
            if k in payload:
                event[k] = payload[k]
        event["el"] = payload.get("el")
        self.log.write(event)

    # ---- navigation ----
    def _on_nav(self, page, frame) -> None:
        if frame != page.main_frame:
            return
        url = frame.url
        if not host_allowed(url, self.allowlist):
            return
        t = now_ms()
        nav = self._spawn(self._write_nav(frame, url, t))
        self._navs.add(nav)
        nav.add_done_callback(self._navs.discard)

    async def _write_nav(self, frame, url: str, t: int) -> None:
        title = None
        try:
            await frame.wait_for_load_state("domcontentloaded", timeout=5000)
            title = await frame.title()
        except Exception:  # noqa: BLE001 - navigated away or closed; keep the URL
            pass
        self.log.write({"t": t, "type": "framenavigated", "url": url, "title": title})

    # ---- network ----
    def _keep(self, request) -> bool:
        if request.resource_type not in ("xhr", "fetch"):
            return False
        if not host_allowed(request.url, self.allowlist):
            return False
        try:
            page_url = request.frame.url
        except Exception:  # noqa: BLE001 - detached frame / service worker
            return True
        return not page_url.startswith("http") or origin(page_url) == origin(request.url)

    def _on_request(self, request) -> None:
        if self._keep(request):
            self._req_t[request] = now_ms()

    def _base_event(self, request) -> dict:
        t = self._req_t.pop(request, now_ms())
        parts = urlsplit(request.url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        headers = request.headers
        req, req_trunc = parse_body(request.post_data, headers.get("content-type", ""), self.sensitive_keys)
        ev = {"t": t, "type": "net", "method": request.method, "url": path, "origin": origin(request.url),
              "status": None, "req": req, "resp": None}
        if req_trunc:
            ev["req_truncated"] = True
        kept = filter_headers(headers)
        if kept:
            ev["req_headers"] = kept
        return ev

    async def _on_finished(self, request) -> None:
        if request not in self._req_t:
            return
        ev = self._base_event(request)
        try:
            resp = await request.response()
        except Exception:  # noqa: BLE001
            resp = None
        if resp is not None:
            ev["status"] = resp.status
            ctype = (await resp.all_headers()).get("content-type", "")
            if any(s in ctype for s in ("json", "text", "xml", "javascript")) or not ctype:
                try:
                    raw = await resp.body()
                    body, trunc = parse_body(raw.decode("utf-8", errors="replace"), ctype, self.sensitive_keys)
                    ev["resp"] = body
                    if trunc:
                        ev["resp_truncated"] = True
                except Exception:  # noqa: BLE001 - body gone (navigation) or redirect
                    pass
            else:
                ev["resp_type"] = ctype
        await self._after_navs()
        self.log.write(ev)

    async def _on_failed(self, request) -> None:
        if request not in self._req_t:
            return
        ev = self._base_event(request)
        ev["error"] = request.failure
        await self._after_navs()
        self.log.write(ev)

    # ---- teardown ----
    async def flush(self, settle_s: float = 0.3) -> None:
        """Flush debounced inputs in every page and let in-flight network events land."""
        if self.context is not None:
            for p in list(self.context.pages):
                try:
                    await p.evaluate("window.__pwFlush && window.__pwFlush()")
                except Exception:  # noqa: BLE001
                    pass
        await asyncio.sleep(settle_s)
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=5)

    def close(self) -> None:
        self.log.close()


def build_recording(dir: Path, meta: dict) -> dict:
    """recording.json content: meta + events from events.jsonl, ordered by time (stable)."""
    events = sorted(read_events(Path(dir) / "events.jsonl"), key=lambda e: e.get("t", 0))
    return {
        "id": meta["id"], "intent": meta.get("intent", ""), "allowlist": meta.get("allowlist", []),
        "started_at": meta.get("started_at"), "stopped_at": meta.get("stopped_at") or now_ms(),
        "start_url": meta.get("start_url"), "events": events,
    }
