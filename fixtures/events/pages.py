"""Event pages for the three-domain wizard smoke (synthetic HTML with JSON-LD Event; no network).

city-hall.html (tickets.cityhall-events.example.com): a plain JSON-LD `Event` and an <h1>.
riverside-arts.html (www.riverside-arts.example.org): an `Event` inside an `@graph` after a WebSite, no <h1>.
night-owl.html (meetups.night-owl.example.net): a third site, learned after TEACH; `Event` after an Organization.
city-hall-2.html: a second city-hall page, sent after the form renamed a question.

    page_record(file, seq)  -> a V1 page record of fixtures/events/<file>
    PAGES                   -> url and expected values per file
    FORM_QUESTIONS          -> questions for demo_apps.tracker_form.create_app(questions=...)
    FORM_TITLE, FORM_BLURB  -> its heading (create_app(title=..., blurb=...))
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from runner.html import canonical_url, css_first, normalize_ws, site_of, visible_text

HERE = Path(__file__).resolve().parent
NAME = "collect_events"
CAPTURED_DATE = "2026-11-14"

PAGES: dict[str, dict[str, Any]] = {
    "city-hall.html": {"url": "https://tickets.cityhall-events.example.com/e/winter-lights-walk?utm_source=mail",
                       "expected": {"name": "Winter Lights Walk", "starts": "2026-12-12T18:30",
                                    "location": "Old Harbor Promenade"}},
    "riverside-arts.html": {"url": "https://www.riverside-arts.example.org/whats-on/quiet-strings",
                            "expected": {"name": "Quiet Strings: an Evening of Duets", "starts": "2027-01-23T19:30",
                                         "location": "Riverside Studio Two"}},
    "night-owl.html": {"url": "https://meetups.night-owl.example.net/m/4411",
                       "expected": {"name": "Lantern Repair Night", "starts": "2027-02-04T20:00",
                                    "location": "The Tinker Shed"}},
    "city-hall-2.html": {"url": "https://tickets.cityhall-events.example.com/e/seed-swap-morning",
                         "expected": {"name": "Seed Swap Morning", "starts": "2027-03-06T09:00",
                                      "location": "North Library Hall"}},
}

# The events form: "Starts" matches nothing by name, so TEACH asks a person where it comes from.
FORM_QUESTIONS = [{"label": lab, "required": lab == "Event Name"}
                  for lab in ("Event Name", "Starts", "Location", "Link", "Date Added")]
FORM_TITLE, FORM_BLURB = "Events to go to", "Every event I might go to, in one list."


def html(file: str) -> str:
    return (HERE / file).read_text()


def page_record(file: str, seq: int = 1, *, captured_at: str = "2026-11-14T15:00:00.000+00:00") -> dict[str, Any]:
    url, page = PAGES[file]["url"], html(file)
    return {"id": f"pg-{seq:06d}", "seq": seq, "url": url, "canonical_url": canonical_url(url, page),
            "site": site_of(url), "title": normalize_ws(css_first(page, "title") or ""), "html": page,
            "text": visible_text(page), "captured_at": captured_at, "captured_date": CAPTURED_DATE}
