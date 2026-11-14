"""`python -m capture record <dir> ...`: launch Chromium, record until SIGTERM/SIGINT, a STOP
file in <dir>, or the user closing the browser; then write recording.json (+ candidates.json)."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

from capture.recorder import Recorder, build_recording, now_ms

STOP_FILE = "STOP"


def read_meta(dir: Path) -> dict:
    try:
        return json.loads((dir / "meta.json").read_text())
    except (OSError, ValueError):
        return {}


def write_meta(dir: Path, meta: dict) -> None:
    tmp = dir / "meta.json.tmp"
    tmp.write_text(json.dumps(meta, indent=2))
    tmp.replace(dir / "meta.json")


def finalize(dir: Path, meta: dict) -> dict:
    """Write recording.json and candidates.json, mark meta stopped. Safe to call twice."""
    meta = {**meta, "status": "stopped", "stopped_at": meta.get("stopped_at") or now_ms()}
    recording = build_recording(dir, meta)
    tmp = dir / "recording.json.tmp"
    tmp.write_text(json.dumps(recording, indent=2, ensure_ascii=False))
    tmp.replace(dir / "recording.json")
    try:
        from matcher import match
        (dir / "candidates.json").write_text(json.dumps(match(recording, base_dir=dir), indent=2, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001 - the recording matters more than the candidates
        print(f"matcher failed: {e!r}", file=sys.stderr)
    meta["n_events"] = len(recording["events"])
    write_meta(dir, meta)
    return recording


async def record(dir: Path, intent: str, allowlist: list[str], start_url: str | None = None,
                 headless: bool = False) -> dict:
    from playwright.async_api import async_playwright

    dir.mkdir(parents=True, exist_ok=True)
    (dir / STOP_FILE).unlink(missing_ok=True)
    meta = read_meta(dir) or {"id": dir.name, "started_at": now_ms()}
    meta.update({"id": meta.get("id") or dir.name, "intent": intent, "allowlist": allowlist,
                 "start_url": start_url, "status": "recording", "pid": os.getpid(), "headless": headless})
    meta.setdefault("started_at", now_ms())
    write_meta(dir, meta)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        browser.on("disconnected", lambda *_: stop.set())
        context = await browser.new_context(viewport=None if not headless else {"width": 1280, "height": 900})
        context.on("close", lambda *_: stop.set())
        rec = Recorder(dir, allowlist)
        await rec.attach(context)

        def watch_page(page):
            page.on("close", lambda *_: (not context.pages) and stop.set())

        context.on("page", watch_page)
        page = await context.new_page()
        if start_url:
            try:
                await page.goto(start_url)
            except Exception as e:  # noqa: BLE001 - keep recording; the user can navigate
                print(f"could not open {start_url}: {e!r}", file=sys.stderr)
        print(json.dumps({"event": "recording", "id": meta["id"], "dir": str(dir)}), flush=True)

        while not stop.is_set():
            if (dir / STOP_FILE).exists():
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.2)
            except asyncio.TimeoutError:
                pass
        if browser.is_connected():
            await rec.flush()
        rec.close()
        meta["stopped_at"] = now_ms()
        try:
            await browser.close()
        except Exception:  # noqa: BLE001 - already gone
            pass
    recording = finalize(dir, meta)
    (dir / STOP_FILE).unlink(missing_ok=True)
    print(json.dumps({"event": "stopped", "id": meta["id"], "n_events": len(recording["events"])}), flush=True)
    return recording
