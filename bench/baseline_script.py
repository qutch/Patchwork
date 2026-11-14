"""Record incoming stock in the Stockroom for every new purchase order in the Supplier Inbox.

Each run:
  1. Fetches messages from the Supplier Inbox  (GET  {INBOX_URL}/api/messages)
  2. Picks out purchase orders from Acme, Globex and Initech and extracts
     PO number, SKU and quantity (each supplier has its own format).
  3. For every PO not yet recorded, checks the Stockroom
     (GET {STOCKROOM_URL}/api/incoming?reference=PO-...) and, if it is not there,
     records it          (POST {STOCKROOM_URL}/api/incoming).

It is safe to run repeatedly (e.g. from cron): a PO is only recorded once, because
the Stockroom itself is checked by reference before every POST, and a small local
state file remembers which messages were already handled.

Usage:
    python baseline_script.py              # process new purchase orders
    python baseline_script.py --dry-run    # show what would be recorded, change nothing
    python baseline_script.py -v           # verbose logging

Environment variables:
    INBOX_URL      default http://localhost:8001
    STOCKROOM_URL  default http://localhost:8002
    PO_STATE_FILE  default ~/.po_to_stockroom_state.json

Example crontab entry (every 5 minutes):
    */5 * * * * /usr/bin/python3 /path/to/baseline_script.py >> /var/log/po_to_stockroom.log 2>&1

Exit code is 0 when everything went fine, 1 if any purchase order could not be
parsed or recorded (so cron/monitoring can alert you); those are retried next run.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional

import httpx

INBOX_URL = os.environ.get("INBOX_URL", "http://localhost:8001").rstrip("/")
STOCKROOM_URL = os.environ.get("STOCKROOM_URL", "http://localhost:8002").rstrip("/")
STATE_FILE = Path(
    os.environ.get("PO_STATE_FILE", str(Path.home() / ".po_to_stockroom_state.json"))
).expanduser()
TIMEOUT = httpx.Timeout(15.0, connect=5.0)

log = logging.getLogger("po_to_stockroom")


# --------------------------------------------------------------------------- parsing

@dataclass(frozen=True)
class PurchaseOrder:
    po_number: str
    sku: str
    quantity: int


PO_RE = r"PO-\d+"
SKU_RE = r"[A-Z]{2,5}-\d{2,6}"
QTY_RE = r"\d[\d,]*"


def _qty(text: str) -> int:
    return int(text.replace(",", ""))


def _norm(text: str) -> str:
    # Normalise line endings and non-breaking spaces that mail systems like to add.
    return text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")


def parse_acme(subject: str, body: str) -> Optional[PurchaseOrder]:
    """Acme:  'PO Number: PO-1022\\nSKU: GAD-217\\nQty: 20'"""
    po = re.search(rf"PO\s*(?:Number|No\.?|#)?\s*[:#]\s*({PO_RE})", body, re.I)
    sku = re.search(rf"\bSKU\s*[:#]?\s*({SKU_RE})", body, re.I)
    qty = re.search(rf"\b(?:Qty|Quantity)\s*[:#]?\s*({QTY_RE})", body, re.I)
    if po and sku and qty:
        return PurchaseOrder(po.group(1).upper(), sku.group(1).upper(), _qty(qty.group(1)))
    return None


def parse_globex(subject: str, body: str) -> Optional[PurchaseOrder]:
    """Globex: 'purchase order PO-2033. Please receive 34 units of item WID-114'"""
    po = re.search(rf"purchase\s+order\s+({PO_RE})", body, re.I)
    item = re.search(rf"receive\s+({QTY_RE})\s+units?\s+of\s+(?:item\s+)?({SKU_RE})", body, re.I)
    if po and item:
        return PurchaseOrder(po.group(1).upper(), item.group(2).upper(), _qty(item.group(1)))
    return None


def parse_initech(subject: str, body: str) -> Optional[PurchaseOrder]:
    """Initech: 'ORDER REF=PO-3030 | PART=GAD-200 | UNITS=77'"""
    po = re.search(rf"\bREF\s*=\s*({PO_RE})", body, re.I)
    sku = re.search(rf"\bPART\s*=\s*({SKU_RE})", body, re.I)
    qty = re.search(rf"\bUNITS\s*=\s*({QTY_RE})", body, re.I)
    if po and sku and qty:
        return PurchaseOrder(po.group(1).upper(), sku.group(1).upper(), _qty(qty.group(1)))
    return None


Parser = Callable[[str, str], Optional[PurchaseOrder]]

# sender domain -> preferred parser. Other parsers are tried as a fallback in
# case a supplier tweaks its template.
SUPPLIERS: dict[str, Parser] = {
    "acme.com": parse_acme,
    "globex.com": parse_globex,
    "initech.com": parse_initech,
}
ALL_PARSERS: list[Parser] = [parse_acme, parse_globex, parse_initech]


def looks_like_po(subject: str, body: str) -> bool:
    text = f"{subject}\n{body}"
    return bool(re.search(PO_RE, text, re.I)) or bool(
        re.search(r"purchase\s+order", text, re.I)
    )


def parse_message(msg: dict) -> tuple[str, Optional[PurchaseOrder]]:
    """Return (kind, po). kind is 'po', 'not_po' or 'unparsed'."""
    domain = (msg.get("sender_domain") or msg.get("sender", "").rpartition("@")[2]).lower()
    subject = _norm(msg.get("subject") or "")
    body = _norm(msg.get("body") or "")

    if domain not in SUPPLIERS:
        return "not_po", None
    if not looks_like_po(subject, body):
        return "not_po", None

    preferred = SUPPLIERS[domain]
    for parser in [preferred] + [p for p in ALL_PARSERS if p is not preferred]:
        po = parser(subject, body)
        if po:
            # Sanity check: if the subject names a PO number, it should agree with the body.
            subj_po = re.search(PO_RE, subject, re.I)
            if subj_po and subj_po.group(0).upper() != po.po_number:
                log.warning("%s: subject says %s but body says %s; using body",
                            msg.get("id"), subj_po.group(0).upper(), po.po_number)
            if po.quantity <= 0:
                return "unparsed", None
            return "po", po
    return "unparsed", None


# --------------------------------------------------------------------------- state

def load_state() -> dict:
    try:
        with STATE_FILE.open() as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("messages"), dict):
            return data
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        log.warning("Could not read state file %s (%s); starting fresh. "
                    "Duplicates are still prevented by checking the Stockroom.", STATE_FILE, e)
    return {"messages": {}}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=STATE_FILE.parent, prefix=".po_state.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.replace(tmp, STATE_FILE)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def single_instance() -> Iterator[bool]:
    """Prevent overlapping cron runs. Yields False if another run holds the lock."""
    lock_path = STATE_FILE.with_suffix(STATE_FILE.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:  # Windows: no locking, just run.
        yield True
        return
    with open(lock_path, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# --------------------------------------------------------------------------- APIs

def fetch_messages(client: httpx.Client) -> list[dict]:
    r = client.get(f"{INBOX_URL}/api/messages")
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise ValueError(f"Unexpected /api/messages response: {type(data).__name__}")
    return data


def already_recorded(client: httpx.Client, reference: str) -> bool:
    r = client.get(f"{STOCKROOM_URL}/api/incoming", params={"reference": reference})
    r.raise_for_status()
    rows = r.json()
    # Filter client-side too, in case the server ignores the query parameter.
    return any(isinstance(row, dict) and row.get("reference") == reference for row in rows)


def product_exists(client: httpx.Client, sku: str) -> Optional[bool]:
    """True/False if we can tell, None if the check itself failed."""
    try:
        r = client.get(f"{STOCKROOM_URL}/api/products/{sku}")
    except httpx.HTTPError as e:
        log.debug("Product lookup for %s failed: %s", sku, e)
        return None
    if r.status_code == 404:
        return False
    if r.is_success:
        return True
    return None


def record_incoming(client: httpx.Client, po: PurchaseOrder) -> dict:
    payload = {"product_id": po.sku, "quantity": po.quantity, "reference": po.po_number}
    r = client.post(f"{STOCKROOM_URL}/api/incoming", json=payload)
    if r.status_code == 422:
        raise RuntimeError(f"Stockroom rejected {payload}: {r.text}")
    r.raise_for_status()
    return r.json() if r.content else {}


# --------------------------------------------------------------------------- main

def run(dry_run: bool) -> int:
    state = load_state()
    seen: dict = state["messages"]
    problems = 0
    recorded = 0

    with httpx.Client(timeout=TIMEOUT, headers={"Accept": "application/json"}) as client:
        try:
            messages = fetch_messages(client)
        except (httpx.HTTPError, ValueError) as e:
            log.error("Could not fetch messages from Supplier Inbox at %s: %s", INBOX_URL, e)
            return 1

        messages.sort(key=lambda m: (m.get("received_at") or "", m.get("id") or ""))
        refs_this_run: set[str] = set()

        for msg in messages:
            mid = msg.get("id")
            if not mid:
                continue
            prev = seen.get(mid, {}).get("status")
            if prev in ("recorded", "duplicate", "not_po"):
                continue

            kind, po = parse_message(msg)
            if kind == "not_po":
                log.debug("%s: not a purchase order (%r)", mid, msg.get("subject"))
                seen[mid] = {"status": "not_po"}
                continue
            if kind == "unparsed" or po is None:
                log.error("%s: looks like a purchase order from %s but could not extract "
                          "PO/SKU/quantity; subject=%r. Will retry next run.",
                          mid, msg.get("sender"), msg.get("subject"))
                seen[mid] = {"status": "unparsed"}
                problems += 1
                continue

            try:
                if po.po_number in refs_this_run or already_recorded(client, po.po_number):
                    log.info("%s: %s already in Stockroom, skipping", mid, po.po_number)
                    seen[mid] = {"status": "duplicate", "reference": po.po_number}
                    continue

                if product_exists(client, po.sku) is False:
                    log.error("%s: %s refers to unknown product %s; not recorded. "
                              "Will retry next run.", mid, po.po_number, po.sku)
                    seen[mid] = {"status": "unknown_product", "reference": po.po_number}
                    problems += 1
                    continue

                if dry_run:
                    log.info("[dry-run] %s: would record %s x%d ref %s",
                             mid, po.sku, po.quantity, po.po_number)
                    continue

                result = record_incoming(client, po)
                refs_this_run.add(po.po_number)
                recorded += 1
                seen[mid] = {"status": "recorded", "reference": po.po_number,
                             "stockroom_id": result.get("id")}
                log.info("%s: recorded %s x%d ref %s (stockroom id %s)",
                         mid, po.sku, po.quantity, po.po_number, result.get("id"))
            except (httpx.HTTPError, RuntimeError, ValueError) as e:
                log.error("%s: failed to record %s: %s. Will retry next run.",
                          mid, po.po_number, e)
                seen[mid] = {"status": "error", "reference": po.po_number}
                problems += 1
            finally:
                if not dry_run:
                    save_state(state)  # persist progress after each PO

    if not dry_run:
        save_state(state)
    log.info("Done: %d recorded, %d problem(s).", recorded, problems)
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="don't write anything")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    with single_instance() as ok:
        if not ok:
            log.info("Another run is in progress; exiting.")
            return 0
        return run(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
