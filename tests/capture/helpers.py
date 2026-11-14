"""Drive a headless Chromium with the recorder attached (the same Recorder the CLI uses)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import urlsplit

from capture.launcher import finalize
from capture.recorder import Recorder, now_ms


def netloc(url: str) -> str:
    return urlsplit(url).netloc


async def record_with(out: Path, allowlist: list[str], script) -> dict:
    """Run `await script(page)` in a recorded headless browser; return the finalized recording."""
    from playwright.async_api import async_playwright

    meta = {"id": out.name, "intent": "test", "allowlist": allowlist, "started_at": now_ms()}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        rec = Recorder(out, allowlist)
        await rec.attach(context)
        page = await context.new_page()
        await script(page)
        await rec.flush()
        rec.close()
        await browser.close()
    return finalize(out, meta)


def run(coro):
    return asyncio.run(coro)
