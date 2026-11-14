"""Research paper pages for the three-domain wizard tests (synthetic HTML; no network).

archive-a.html (papers.open-archive.example.org): Highwire `citation_*` meta, og: meta and ScholarlyArticle JSON-LD.
proceedings-b.html (proceedings.hub.example.net): `citation_*` and og: meta only, no JSON-LD and no <h1>.

    page_record(file, seq)  -> a V1 page record of fixtures/papers/<file>
    PAGES                   -> url and expected values per file
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from runner.html import canonical_url, css_first, normalize_ws, site_of, visible_text

HERE = Path(__file__).resolve().parent
NAME = "collect_papers"
CAPTURED_DATE = "2026-11-14"

PAGES: dict[str, dict[str, Any]] = {
    "archive-a.html": {"url": "https://papers.open-archive.example.org/abs/2506.01234?context=cs",
                       "expected": {"title": "Sparse Routing for Tiny Transformers", "year": 2025,
                                    "venue": "Journal of Synthetic Learning"}},
    "proceedings-b.html": {"url": "https://proceedings.hub.example.net/wfa-2024/paper/17",
                           "expected": {"title": "Counting Birds with Cheap Microphones", "year": 2024,
                                        "venue": "Workshop on Field Acoustics"}},
}


def html(file: str) -> str:
    return (HERE / file).read_text()


def page_record(file: str, seq: int = 1, *, captured_at: str = "2026-11-14T15:00:00.000+00:00") -> dict[str, Any]:
    url, page = PAGES[file]["url"], html(file)
    return {"id": f"pg-{seq:06d}", "seq": seq, "url": url, "canonical_url": canonical_url(url, page),
            "site": site_of(url), "title": normalize_ws(css_first(page, "title") or ""), "html": page,
            "text": visible_text(page), "captured_at": captured_at, "captured_date": CAPTURED_DATE}


def body(file: str) -> dict[str, Any]:
    """The nested body the papers API should receive for this page."""
    exp = PAGES[file]["expected"]
    return {"title": exp["title"], "year": exp["year"], "venue": {"name": exp["venue"]}}
