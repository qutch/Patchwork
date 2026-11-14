"""Product pages for the V15 generalization tests (synthetic HTML with JSON-LD Product; no network).

    page_record(file, seq)  -> a V1 page record of fixtures/products/<file>
    examples()              -> golden examples for fixtures/products/spec.yaml (built here, not stored)
    FORM_QUESTIONS          -> questions for demo_apps.tracker_form.create_app(questions=...)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from runner.html import canonical_url, css_first, normalize_ws, site_of, visible_text

HERE = Path(__file__).resolve().parent
SPEC = HERE / "spec.yaml"
NAME = "track_product_prices"
CAPTURED_DATE = "2026-11-14"

PAGES: dict[str, dict[str, Any]] = {
    "shop-a.html": {"url": "https://shop-a.example.com/p/acme-steel-kettle?ref=home#reviews",
                    "expected": {"name": "Acme Steel Kettle 1.7 L", "price": "24.99", "released": "2026-03-01",
                                 "image": "https://shop-a.example.com/img/kettle-1.jpg"}},
    "shop-b.html": {"url": "https://lumen-supply.example.org/lamps/bolt",
                    "expected": {"name": "Bolt Desk Lamp", "price": "89.00", "released": "2025-11-20",
                                 "image": "https://cdn.lumen-supply.example.org/bolt.png"}},
}

FORM_QUESTIONS = [{"label": lab, "required": lab == "Product"}
                  for lab in ("Product", "Price", "Released", "Image", "Link", "Date Added")]


def html(file: str) -> str:
    return (HERE / file).read_text()


def page_record(file: str, seq: int = 1, *, captured_at: str = "2026-11-14T15:00:00.000+00:00") -> dict[str, Any]:
    url, page = PAGES[file]["url"], html(file)
    return {"id": f"pg-{seq:06d}", "seq": seq, "url": url, "canonical_url": canonical_url(url, page),
            "site": site_of(url), "title": normalize_ws(css_first(page, "title") or ""), "html": page,
            "text": visible_text(page), "captured_at": captured_at, "captured_date": CAPTURED_DATE}


def body(file: str, rec: dict[str, Any]) -> dict[str, str]:
    exp = PAGES[file]["expected"]
    return {"Product": exp["name"], "Price": exp["price"], "Released": exp["released"], "Image": exp["image"],
            "Link": rec["canonical_url"], "Date Added": rec["captured_date"]}


def examples() -> list[dict[str, Any]]:
    rec = page_record("shop-a.html", 1)
    return [{"name": "page_shop_a", "record": rec, "expect": {"body": body("shop-a.html", rec)}}]
