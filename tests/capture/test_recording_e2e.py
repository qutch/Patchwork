"""Headless end-to-end recording: open an Inbox message, copy the PO, fill the Stockroom
"Add incoming stock" form, submit. The recording must hold the POST /api/incoming network
event and the DOM events, and the matcher must explain all 3 destination fields."""

from __future__ import annotations

import json
import re

from tests.capture.helpers import netloc, record_with, run

SELECT_PO = """() => {
  const pre = document.querySelector('[data-testid=message-body]');
  const node = pre.firstChild, text = node.textContent;
  const m = /PO Number: (PO-\\d+)/.exec(text);
  const start = m.index + m[0].length - m[1].length;
  const r = document.createRange(); r.setStart(node, start); r.setEnd(node, start + m[1].length);
  const s = getSelection(); s.removeAllRanges(); s.addRange(r);
  document.execCommand('copy');
  return m[1];
}"""

PASTE = """([sel, text]) => {
  const el = document.querySelector(sel); el.focus();
  const dt = new DataTransfer(); dt.setData('text/plain', text);
  el.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
}"""


def _demonstrate(apps, seen):
    async def script(page):
        await page.goto(apps["inbox"] + "/")
        link = page.get_by_role("link", name=re.compile(r"^Purchase Order PO-")).first
        await link.wait_for()
        await link.click()
        await page.wait_for_selector("[data-testid=message-body][data-loaded]")
        body = await page.inner_text("[data-testid=message-body]")
        seen["po"] = await page.evaluate(SELECT_PO)
        seen["sku"] = re.search(r"SKU: (\S+)", body).group(1)
        seen["qty"] = int(re.search(r"Qty: (\d+)", body).group(1))
        seen["message_id"] = page.url.rsplit("/", 1)[1]

        await page.goto(apps["live"] + "/incoming/new")
        await page.select_option("[data-testid=product]", seen["sku"])
        await page.fill("[data-testid=quantity]", str(seen["qty"]))
        await page.evaluate(PASTE, ["[data-testid=reference]", seen["po"]])
        await page.fill("[data-testid=reference]", seen["po"])
        await page.click("button[type=submit]")
        await page.wait_for_function("document.getElementById('status').textContent === 'Saved'")
    return script


def test_headless_recording_end_to_end(apps, workspace):
    out = workspace / "recordings" / "rec-e2e"
    seen: dict = {}
    allow = [netloc(apps["inbox"]), netloc(apps["live"])]
    recording = run(record_with(out, allow, _demonstrate(apps, seen)))
    events = recording["events"]
    by_type = lambda t: [e for e in events if e["type"] == t]  # noqa: E731

    # Files: live log + final recording, same events
    assert (out / "events.jsonl").read_text().count("\n") == len(events)
    assert json.loads((out / "recording.json").read_text())["events"] == events
    assert [e["t"] for e in events] == sorted(e["t"] for e in events)

    # Network: the source GETs and the sink POST, with bodies
    nets = by_type("net")
    detail = next(e for e in nets if e["url"] == f"/api/messages/{seen['message_id']}")
    assert detail["method"] == "GET" and detail["status"] == 200 and detail["resp"]["id"] == seen["message_id"]
    assert any(e["url"] == "/api/messages" and e["method"] == "GET" for e in nets)
    post = next(e for e in nets if e["method"] == "POST")
    assert post["url"] == "/api/incoming" and post["status"] == 201 and post["origin"] == apps["live"]
    assert post["req"] == {"product_id": seen["sku"], "quantity": seen["qty"], "reference": seen["po"]}
    assert post["resp"]["reference"] == seen["po"]

    # Navigation
    navs = [e["url"] for e in by_type("framenavigated")]
    assert navs == [apps["inbox"] + "/", f"{apps['inbox']}/messages/{seen['message_id']}",
                    apps["live"] + "/incoming/new"]
    assert by_type("framenavigated")[2]["title"] == "Add incoming stock"

    # DOM events with element descriptors
    click = by_type("click")[0]
    assert click["el"]["tag"] == "a" and click["el"]["role"] == "link"
    assert click["el"]["name"].startswith("Purchase Order PO-")
    copy = by_type("copy")[0]
    assert copy["text"] == seen["po"] and copy["el"]["testid"] == "message-body" and copy["el"]["nearby"] == "PO Number:"
    change = by_type("change")[0]
    assert change["value"] == seen["sku"]
    assert {k: change["el"][k] for k in ("tag", "role", "name", "label", "testid")} == \
        {"tag": "select", "role": "combobox", "name": "product_id", "label": "Product", "testid": "product"}
    inputs = by_type("input")
    assert [(e["el"]["name"], e["value"]) for e in inputs] == [("quantity", str(seen["qty"]))]
    q = inputs[0]["el"]
    assert (q["role"], q["label"], q["testid"], q["css"]) == ("spinbutton", "Quantity", "quantity",
                                                              "form#f input[name=quantity]")
    paste = by_type("paste")[0]
    assert paste["value"] == seen["po"] and paste["el"]["name"] == "reference"
    submit = by_type("submit")[0]
    assert submit["el"]["tag"] == "form" and submit["el"]["css"] == "form#f" and submit["el"]["nearby"] == "Save"
    assert submit["t"] <= post["t"]
    order = [e["type"] for e in events if e["type"] in ("copy", "change", "input", "paste", "submit")]
    assert order == ["copy", "change", "input", "paste", "submit"]

    # Matcher (written by finalize) explains all three destination fields with working regexes
    cands = json.loads((out / "candidates.json").read_text())
    assert cands["sink"]["path"] == "/api/incoming" and cands["sink"]["base_url"] == apps["live"]
    assert cands["source"]["base_url"] == apps["inbox"]
    assert cands["source"]["list"] == {"method": "GET", "path": "/api/messages"}
    assert cands["source"]["record"]["id"] == seen["message_id"]
    assert cands["unexplained"] == []
    mappings = {m["dest_field"]: m for m in cands["mappings"]}
    assert set(mappings) == {"product_id", "quantity", "reference"}
    body = cands["source"]["record"]["body"]
    expected = {"product_id": seen["sku"], "quantity": str(seen["qty"]), "reference": seen["po"]}
    for field, m in mappings.items():
        assert m["kind"] == "text" and m["source_field"] == "body" and m["regex_ok"], m
        assert re.search(m["candidate_regex"], body).group(1) == expected[field]
    assert mappings["quantity"]["cast"] == "int" and mappings["quantity"]["label"] == "Qty:"
    assert mappings["reference"].get("copied") is True
