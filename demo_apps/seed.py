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
