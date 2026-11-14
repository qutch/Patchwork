"""Deterministic seed for the demo: ~150 free-text PO messages from 3 supplier formats,
with duplicates, invalid quantities, unknown SKUs and a little junk mixed in.

`generate()` is pure, so the Inbox app, the benchmark and the tests all see the same
data. `python -m demo_apps.seed` also writes it to workspace/seed/ for inspection.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CATALOG: dict[str, str] = {
    **{f"WID-{n}": f"Widget {n}" for n in range(100, 130)},
    **{f"GAD-{n}": f"Gadget {n}" for n in range(200, 220)},
}
UNKNOWN_SKUS = ["WID-999", "GAD-404", "ZZZ-001"]

SUPPLIERS = {
    "acme.com": {"sender": "orders@acme.com", "po_base": 1000},
    "globex.com": {"sender": "procurement@globex.com", "po_base": 2000},
    "initech.com": {"sender": "erp-noreply@initech.com", "po_base": 3000},
}

# Which warehouse each supplier ships to; used by the v2 contract (Break 1).
SUPPLIER_WAREHOUSE = {"acme.com": "BOS", "globex.com": "NYC", "initech.com": "BOS"}


def render(domain: str, po: str, sku: str, qty: int) -> tuple[str, str]:
    """Return (subject, body) in the supplier's house format."""
    if domain == "acme.com":
        return (
            f"Purchase Order {po}",
            f"Hi team,\n\nPlease find our order below.\n\n"
            f"PO Number: {po}\nSKU: {sku}\nQty: {qty}\n\nThanks,\nAcme Supply",
        )
    if domain == "globex.com":
        return (
            f"Globex order {po}",
            f"Hello Stockroom,\n\nThis is Globex purchase order {po}. Please receive {qty} units "
            f"of item {sku} at your earliest convenience.\n\nRegards,\nGlobex Procurement",
        )
    if domain == "initech.com":
        return (
            f"[ERP] {po}",
            f"ORDER REF={po} | PART={sku} | UNITS={qty}\nSent automatically from Initech ERP",
        )
    raise ValueError(domain)


JUNK = [
    ("news@acme.com", "acme.com", "Acme holiday hours",
     "Our office will be closed on Thursday and Friday for the holiday. Orders resume Monday."),
    ("procurement@globex.com", "globex.com", "Out of office",
     "I am out of the office until Monday with limited access to email."),
]


@dataclass
class Seed:
    messages: list[dict[str, Any]]
    ground_truth: dict[str, dict[str, Any]]
    catalog: dict[str, str] = field(default_factory=lambda: dict(CATALOG))


def generate(seed: int = 42, valid_per_supplier: int = 40, n_dupes: int = 10,
             n_invalid_qty: int = 9, n_unknown_sku: int = 9) -> Seed:
    rng = random.Random(seed)
    known = sorted(CATALOG)
    counters = {d: 0 for d in SUPPLIERS}

    def next_po(domain: str) -> str:
        counters[domain] += 1
        return f"PO-{SUPPLIERS[domain]['po_base'] + counters[domain]}"

    # (domain, po, sku, qty, expected) — expected outcome before dedupe is applied
    items: list[dict[str, Any]] = []
    for domain in SUPPLIERS:
        for _ in range(valid_per_supplier):
            items.append({"domain": domain, "po": next_po(domain), "sku": rng.choice(known),
                          "qty": rng.randint(1, 120), "expect": ("success", None)})
    domains = list(SUPPLIERS)
    for i in range(n_invalid_qty):
        d = domains[i % 3]
        items.append({"domain": d, "po": next_po(d), "sku": rng.choice(known), "qty": 0,
                      "expect": ("quarantined", "VALIDATION_FAILED")})
    for i in range(n_unknown_sku):
        d = domains[i % 3]
        items.append({"domain": d, "po": next_po(d), "sku": UNKNOWN_SKUS[i % len(UNKNOWN_SKUS)],
                      "qty": rng.randint(1, 120), "expect": ("quarantined", "NOT_FOUND")})
    for sender, d, subject, body in JUNK:
        items.append({"domain": d, "junk": (sender, subject, body), "expect": ("quarantined", "PARSE_NO_MATCH")})
    rng.shuffle(items)

    # Resends of valid POs, each placed somewhere after its original.
    valid_idx = [i for i, it in enumerate(items) if it["expect"][0] == "success"]
    for orig in sorted(rng.sample(valid_idx, n_dupes), reverse=True):
        dup = dict(items[orig], expect=("duplicate", None), resend=True)
        items.insert(rng.randint(orig + 1, len(items)), dup)

    t = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
    messages, truth = [], {}
    for n, it in enumerate(items, start=1):
        t += timedelta(minutes=rng.randint(3, 40), seconds=rng.randint(0, 59))
        mid = f"msg-{n:04d}"
        if "junk" in it:
            sender, subject, body = it["junk"]
        else:
            sender = SUPPLIERS[it["domain"]]["sender"]
            subject, body = render(it["domain"], it["po"], it["sku"], it["qty"])
            if it.get("resend"):
                subject = f"RESEND: {subject}"
        messages.append({
            "id": mid, "sender": sender, "sender_domain": it["domain"], "subject": subject,
            "body": body, "received_at": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        outcome, reason = it["expect"]
        gt: dict[str, Any] = {"outcome": outcome, "reason": reason}
        if outcome == "success":
            gt["body"] = {"product_id": it["sku"], "quantity": it["qty"], "reference": it["po"]}
            gt["body_v2"] = {"sku": it["sku"], "quantity_delta": it["qty"], "reference": it["po"],
                             "warehouse_id": SUPPLIER_WAREHOUSE[it["domain"]]}
        truth[mid] = gt
    return Seed(messages=messages, ground_truth=truth)


# --------------------------------------------------------------------------------------
# Live deliveries (Inbox POST /admin/deliver and /admin/new_supplier).
# Added after generate() so the 150-message seed above stays byte-for-byte unchanged.
# --------------------------------------------------------------------------------------

DELIVERY_BLOCK = 50  # every block of 50 deliveries has exactly the seed's mix ratio
DELIVERY_MIX = {"valid": 40, "resend": 3, "invalid_qty": 3, "unknown_sku": 3, "junk": 1}
DELIVERY_PO_OFFSET = 100  # delivered POs start at PO-1101 / PO-2101 / PO-3101

NEW_SUPPLIER = {"domain": "umbrella.com", "sender": "purchasing@umbrella.com", "po_base": 4000,
                "warehouse": "NYC"}


def render_new_supplier(po: str, sku: str, cases: int, per_case: int) -> tuple[str, str]:
    """Break 2: a fourth supplier quoting cases, not units."""
    return (
        f"Umbrella order {po}",
        f"Good morning,\n\nPlease ship the following against our reference {po}.\n\n"
        f"Item code: {sku} / {cases} cases ({per_case}/case)\n\nBest regards,\nUmbrella Purchasing",
    )


def _next_time(after: str, rng: random.Random) -> str:
    t = datetime.strptime(after, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    t += timedelta(minutes=rng.randint(3, 40), seconds=rng.randint(0, 59))
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_valid_items(seed: int) -> list[dict[str, Any]]:
    """Seed POs that end as success, as delivery items (resend candidates for the first block)."""
    s = generate(seed=seed)
    out = []
    for m in s.messages:
        gt = s.ground_truth[m["id"]]
        if gt["outcome"] == "success":
            b = gt["body"]
            out.append({"domain": m["sender_domain"], "po": b["reference"], "sku": b["product_id"],
                        "qty": b["quantity"], "expect": ("success", None)})
    return out


def _delivery_items(n: int, seed: int) -> list[dict[str, Any]]:
    """The first n items of the deterministic delivery stream (independent of timing)."""
    known = sorted(CATALOG)
    domains = list(SUPPLIERS)
    counters = {d: 0 for d in SUPPLIERS}
    seed_valid: list[dict[str, Any]] | None = None
    items: list[dict[str, Any]] = []
    block = 0
    while len(items) < n:
        rng = random.Random(f"deliver:{seed}:{block}")
        kinds = [k for k, c in DELIVERY_MIX.items() for _ in range(c)]
        rng.shuffle(kinds)
        for j, kind in enumerate(kinds):
            d = domains[(block * DELIVERY_BLOCK + j) % 3]
            if kind == "resend":
                pool = [it for it in items if it["expect"][0] == "success"]
                if not pool:
                    seed_valid = seed_valid or _seed_valid_items(seed)
                    pool = seed_valid
                items.append(dict(rng.choice(pool), expect=("duplicate", None), resend=True))
            elif kind == "junk":
                sender, jd, subject, body = JUNK[block % len(JUNK)]
                items.append({"domain": jd, "junk": (sender, subject, body),
                              "expect": ("quarantined", "PARSE_NO_MATCH")})
            else:
                counters[d] += 1
                po = f"PO-{SUPPLIERS[d]['po_base'] + DELIVERY_PO_OFFSET + counters[d]}"
                if kind == "valid":
                    items.append({"domain": d, "po": po, "sku": rng.choice(known), "qty": rng.randint(1, 120),
                                  "expect": ("success", None)})
                elif kind == "invalid_qty":
                    items.append({"domain": d, "po": po, "sku": rng.choice(known), "qty": 0,
                                  "expect": ("quarantined", "VALIDATION_FAILED")})
                else:
                    items.append({"domain": d, "po": po, "sku": UNKNOWN_SKUS[(block + j) % len(UNKNOWN_SKUS)],
                                  "qty": rng.randint(1, 120), "expect": ("quarantined", "NOT_FOUND")})
        block += 1
    return items[:n]


def deliveries(start: int, count: int, after: str, first_id: int, seed: int = 42) -> Seed:
    """Items `start .. start+count-1` (0-based) of the delivery stream: the same 3 formats and
    the seed's mix (valid, resends, zero qty, unknown SKU, junk). Received after `after`;
    ids continue from `first_id`. Ground truth says what the v1 spec should do with each."""
    items = _delivery_items(start + count, seed)[start:]
    rng = random.Random(f"deliver-times:{seed}:{start}:{after}")
    messages, truth = [], {}
    t = after
    for k, it in enumerate(items):
        t = _next_time(t, rng)
        mid = f"msg-{first_id + k:04d}"
        if "junk" in it:
            sender, subject, body = it["junk"]
        else:
            sender = SUPPLIERS[it["domain"]]["sender"]
            subject, body = render(it["domain"], it["po"], it["sku"], it["qty"])
            if it.get("resend"):
                subject = f"RESEND: {subject}"
        messages.append({"id": mid, "sender": sender, "sender_domain": it["domain"], "subject": subject,
                         "body": body, "received_at": t})
        outcome, reason = it["expect"]
        gt: dict[str, Any] = {"outcome": outcome, "reason": reason, "delivered": True}
        if outcome == "success":
            gt["body"] = {"product_id": it["sku"], "quantity": it["qty"], "reference": it["po"]}
            gt["body_v2"] = {"sku": it["sku"], "quantity_delta": it["qty"], "reference": it["po"],
                             "warehouse_id": SUPPLIER_WAREHOUSE[it["domain"]]}
        truth[mid] = gt
    return Seed(messages=messages, ground_truth=truth)


def new_supplier_messages(start: int, count: int, after: str, first_id: int, seed: int = 42) -> Seed:
    """Break 2 messages from umbrella.com. The v1 spec has no parser for them, so their
    ground truth is quarantined:PARSE_NO_MATCH; `intended` holds the body a repaired spec
    should send (quantity = cases * per_case)."""
    known = sorted(CATALOG)
    rng = random.Random(f"new-supplier-times:{seed}:{start}:{after}")
    messages, truth = [], {}
    t = after
    for k in range(count):
        n = start + k
        item_rng = random.Random(f"new-supplier:{seed}:{n}")
        po = f"PO-{NEW_SUPPLIER['po_base'] + n + 1}"
        sku = item_rng.choice(known)
        cases, per_case = item_rng.randint(1, 10), item_rng.choice([6, 12, 24])
        subject, body = render_new_supplier(po, sku, cases, per_case)
        t = _next_time(t, rng)
        mid = f"msg-{first_id + k:04d}"
        messages.append({"id": mid, "sender": NEW_SUPPLIER["sender"], "sender_domain": NEW_SUPPLIER["domain"],
                         "subject": subject, "body": body, "received_at": t})
        qty = cases * per_case
        truth[mid] = {
            "outcome": "quarantined", "reason": "PARSE_NO_MATCH", "delivered": True, "new_supplier": True,
            "intended": {
                "body": {"product_id": sku, "quantity": qty, "reference": po},
                "body_v2": {"sku": sku, "quantity_delta": qty, "reference": po,
                            "warehouse_id": NEW_SUPPLIER["warehouse"]},
                "cases": cases, "per_case": per_case,
            },
        }
    return Seed(messages=messages, ground_truth=truth)


def main() -> None:
    out = Path("workspace/seed")
    out.mkdir(parents=True, exist_ok=True)
    s = generate()
    (out / "inbox.json").write_text(json.dumps(s.messages, indent=2))
    (out / "ground_truth.json").write_text(json.dumps(s.ground_truth, indent=2))
    (out / "catalog.json").write_text(json.dumps(s.catalog, indent=2))
    counts: dict[str, int] = {}
    for g in s.ground_truth.values():
        k = g["outcome"] + (f":{g['reason']}" if g["reason"] else "")
        counts[k] = counts.get(k, 0) + 1
    print(f"wrote {len(s.messages)} messages to {out}/  ground truth: {counts}")


if __name__ == "__main__":
    main()
