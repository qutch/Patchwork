"""Inbox admin endpoints: deliver, new_supplier (Break 2), reset, ground_truth."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter

from fastapi.testclient import TestClient

from demo_apps.inbox.app import create_app
from demo_apps.seed import generate

SEED_SHA256 = "0ba9c1ca5bfb7bff9e45963ddff353dda2348ae190fe80b6d17fb145a400d100"
UMBRELLA = re.compile(r"Item code: ([A-Z]+-\d+) / (\d+) cases \((\d+)/case\)")


def _client() -> TestClient:
    return TestClient(create_app())


def test_seed_is_unchanged():
    s = generate()
    digest = hashlib.sha256(json.dumps([s.messages, s.ground_truth], sort_keys=True).encode()).hexdigest()
    assert digest == SEED_SHA256 and len(s.messages) == 150


def test_deliver_appends_deterministic_messages_with_the_seed_mix():
    c = _client()
    seed = generate()
    latest = max(m["received_at"] for m in seed.messages)
    r = c.post("/admin/deliver", json={"count": 50}).json()
    assert r["total"] == 200 and r["delivered"] == [f"msg-{n:04d}" for n in range(151, 201)]
    msgs = c.get("/api/messages", params={"since": latest}).json()
    assert [m["id"] for m in msgs] == r["delivered"]
    times = [m["received_at"] for m in msgs]
    assert times == sorted(times) and len(set(times)) == 50

    truth = c.get("/admin/ground_truth").json()
    assert len(truth) == 200
    assert all(truth[m["id"]] == seed.ground_truth[m["id"]] for m in seed.messages)
    mix = Counter(truth[m["id"]]["outcome"] + ":" + str(truth[m["id"]]["reason"]) for m in msgs)
    assert mix == {"success:None": 40, "duplicate:None": 3, "quarantined:VALIDATION_FAILED": 3,
                   "quarantined:NOT_FOUND": 3, "quarantined:PARSE_NO_MATCH": 1}
    assert set(m["sender_domain"] for m in msgs) == {"acme.com", "globex.com", "initech.com"}
    for m in msgs:
        gt = truth[m["id"]]
        if gt["outcome"] == "success":
            b = gt["body"]
            assert b["reference"] in m["body"] and b["product_id"] in m["body"] and str(b["quantity"]) in m["body"]
            assert gt["body_v2"]["reference"] == b["reference"]

    # New POs never collide with seed POs; resends reuse a PO that was already a success.
    seed_pos = {g["body"]["reference"] for g in seed.ground_truth.values() if g.get("body")}
    new_pos = [truth[m["id"]]["body"]["reference"] for m in msgs if truth[m["id"]].get("body")]
    assert not seed_pos & set(new_pos) and len(set(new_pos)) == 40

    # Deterministic: a fresh app delivering 20 + 30 sees the same messages (ids, content).
    c2 = _client()
    c2.post("/admin/deliver", json={"count": 20})
    c2.post("/admin/deliver", json={"count": 30})
    msgs2 = c2.get("/api/messages", params={"since": latest}).json()
    strip = lambda ms: [(m["id"], m["sender"], m["subject"], m["body"]) for m in ms]  # noqa: E731
    assert strip(msgs2) == strip(msgs)


def test_new_supplier_break_2_messages():
    c = _client()
    c.post("/admin/deliver", json={"count": 2})
    r = c.post("/admin/new_supplier", json={"count": 4}).json()
    assert r["delivered"] == ["msg-0153", "msg-0154", "msg-0155", "msg-0156"]
    truth = c.get("/admin/ground_truth").json()
    msgs = [c.get(f"/api/messages/{i}").json() for i in r["delivered"]]
    prev = c.get("/api/messages/msg-0152").json()["received_at"]
    for m in msgs:
        assert m["sender"] == "purchasing@umbrella.com" and m["sender_domain"] == "umbrella.com"
        assert m["received_at"] > prev
        prev = m["received_at"]
        sku, cases, per_case = UMBRELLA.search(m["body"]).groups()
        po = re.search(r"PO-4\d{3}", m["body"]).group()
        gt = truth[m["id"]]
        assert (gt["outcome"], gt["reason"]) == ("quarantined", "PARSE_NO_MATCH")
        assert gt["intended"]["body"] == {"product_id": sku, "quantity": int(cases) * int(per_case), "reference": po}
    # the next call continues the PO sequence
    nxt = c.post("/admin/new_supplier", json={"count": 1}).json()["delivered"][0]
    assert "PO-4005" in c.get(f"/api/messages/{nxt}").json()["body"]


def test_reset_restores_the_seed():
    c = _client()
    c.post("/admin/deliver", json={"count": 5})
    c.post("/admin/new_supplier", json={"count": 3})
    assert c.post("/admin/reset").json() == {"ok": True, "total": 150}
    assert c.get("/api/messages").json() == generate().messages
    assert c.get("/admin/ground_truth").json() == generate().ground_truth
    # after a reset, deliveries start over from the same point
    assert c.post("/admin/deliver", json={"count": 1}).json()["delivered"] == ["msg-0151"]


def test_count_validation_and_default():
    c = _client()
    assert c.post("/admin/deliver", json={"count": 0}).status_code == 422
    assert c.post("/admin/deliver").json()["total"] == 151


def test_html_pages_render_from_the_api():
    c = _client()
    assert "fetch('/api/messages')" in c.get("/").text
    page = c.get("/messages/msg-0007").text
    assert "<title>Purchase Order PO-1022</title>" in page and 'data-testid="message-body"' in page
    assert c.get("/messages/nope").status_code == 404
