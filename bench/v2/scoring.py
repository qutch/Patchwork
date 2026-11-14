"""Scoring for the v2 benchmark (docs/contracts.md V12). Pure functions, no I/O.

Scoring is by what actually landed in the tracker form (its rows), not by what Patchwork says it did, as in v1's
bench/scoring.py. Per posting, by the rows whose Link equals the posting's canonical URL:

  right_write   exactly one row, title and company equal the answer key        correct = True
  wrong_write   a row with another title or company, or a second row           correct = False, bad write
  held          no row, and Patchwork held it for a person                     correct = None (not wrong, not done)
  dropped       no row, and not held (silently lost)                           correct = False

Values are compared after whitespace normalisation and case folding; an answer-key entry may list extra accepted
spellings in `accept: {field: [..]}`.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

KINDS = ("measured", "measured (earlier run)", "estimate", "extrapolated", "not measured")
OUTCOMES = ("right_write", "wrong_write", "held", "dropped")
TITLE_FIELDS = ("Job Title",)
COMPANY_FIELDS = ("Company", "Employer")  # Employer after the demo's rename beat
LINK_FIELDS = ("Link",)


def norm(s: Any) -> str:
    return " ".join(str(s if s is not None else "").split()).casefold()


def values_match(got: Any, want: Any, accept: Iterable[str] = ()) -> bool:
    if want is None or norm(want) == "":
        return False
    return norm(got) in {norm(want), *(norm(a) for a in accept)}


def _first(row: dict, names: Iterable[str]) -> Any:
    for n in names:
        if n in row:
            return row[n]
    return None


def rows_for(rows: Iterable[dict], link: str) -> list[dict]:
    want = norm(link)
    return [r for r in rows if norm(_first(r, LINK_FIELDS)) == want]


def score_posting(entry: dict, link: str, rows: Iterable[dict], held: bool) -> dict[str, Any]:
    """`entry`: answer-key row; `link`: the canonical URL Patchwork stored for the page; `rows`: every form row;
    `held`: Patchwork reported the page held for a person (label held, or a learn waiting for approval)."""
    mine = rows_for(rows, link)
    accept = entry.get("accept") or {}
    got = None
    if not mine:
        outcome = "held" if held else "dropped"
    else:
        first = mine[0]
        got = {"title": _first(first, TITLE_FIELDS), "company": _first(first, COMPANY_FIELDS)}
        ok = (values_match(got["title"], entry["title"], accept.get("title", ()))
              and values_match(got["company"], entry["company"], accept.get("company", ())))
        outcome = "right_write" if ok and len(mine) == 1 else "wrong_write"
    return {
        "outcome": outcome,
        "correct": {"right_write": True, "wrong_write": False, "held": None, "dropped": False}[outcome],
        "bad_write": outcome == "wrong_write",
        "rows": len(mine),
        "got": got,
        "want": {"title": entry["title"], "company": entry["company"]},
    }


def summarize(scored: list[dict]) -> dict[str, Any]:
    n = len(scored)
    counts = {o: sum(s["outcome"] == o for s in scored) for o in OUTCOMES}
    return {"postings": n, **counts,
            "accuracy": round(counts["right_write"] / n, 4) if n else None,
            "wrong_rows_written": counts["wrong_write"]}


# ---------------------------------------------------------------------------------------------
# AI-call series

def cumulative(xs: Iterable[float]) -> list[float]:
    out, total = [], 0
    for x in xs:
        total += x
        out.append(total)
    return out


def patchwork_calls(postings: list[dict], per_learn: int | None) -> list[int]:
    """AI calls attributed to each posting: the learn thread's own model_calls when it reported a positive count
    (deep agent), else `per_learn` for a posting that started a learn thread (scripted agent: the per-learn count of
    an earlier deep run, or the design budget), else 0. A posting read by a known reader costs 0."""
    out = []
    for p in postings:
        if not p.get("learn_thread_id"):
            out.append(0)
        elif (p.get("thread_model_calls") or 0) > 0:
            out.append(int(p["thread_model_calls"]))
        else:
            out.append(int(per_learn or 0))
    return out


def browser_agent_calls(n: int, per_posting: int) -> list[int]:
    return [per_posting] * n


# ---------------------------------------------------------------------------------------------
# labels

def metric(name: str, value: Any, unit: str, kind: str, source: str, note: str | None = None) -> dict:
    if kind not in KINDS:
        raise ValueError(f"{name}: kind {kind!r} not in {KINDS}")
    if not source:
        raise ValueError(f"{name}: no source")
    if (value is None) != (kind == "not measured"):
        raise ValueError(f"{name}: value {value!r} with kind {kind!r}")
    m = {"name": name, "value": value, "unit": unit, "kind": kind, "source": source}
    if note:
        m["note"] = note
    return m


def check_results(results: dict) -> list[str]:
    """Problems that make results.json dishonest or off-contract; [] when fine."""
    bad: list[str] = []
    for i, m in enumerate(results.get("numbers") or []):
        where = f"numbers[{i}] {m.get('name')!r}"
        if m.get("kind") not in KINDS:
            bad.append(f"{where}: kind {m.get('kind')!r}")
        if not m.get("source"):
            bad.append(f"{where}: no source")
        if not m.get("unit"):
            bad.append(f"{where}: no unit")
        if (m.get("value") is None) != (m.get("kind") == "not measured"):
            bad.append(f"{where}: value/kind mismatch")
    series = results.get("series") or {}
    meta = results.get("series_meta") or {}
    for key in ("patchwork_cumulative_ai_calls", "browser_agent_cumulative_ai_calls"):
        if key not in series:
            bad.append(f"series.{key}: missing")
            continue
        if len(series[key]) != len(results.get("postings") or []):
            bad.append(f"series.{key}: {len(series[key])} points for {len(results.get('postings') or [])} postings")
        m = meta.get(key) or {}
        if m.get("kind") not in KINDS or not m.get("source"):
            bad.append(f"series_meta.{key}: needs kind and source")
    if (meta.get("browser_agent_cumulative_ai_calls") or {}).get("kind") == "estimate" and not \
            (meta.get("browser_agent_cumulative_ai_calls") or {}).get("assumption"):
        bad.append("series_meta.browser_agent_cumulative_ai_calls: an estimate needs its assumption")
    for i, p in enumerate(results.get("postings") or []):
        for k in ("n", "file", "site", "label", "ai_calls", "seconds", "correct"):
            if k not in p:
                bad.append(f"postings[{i}]: missing {k}")
        if p.get("ai_calls_kind") not in KINDS:
            bad.append(f"postings[{i}]: ai_calls_kind {p.get('ai_calls_kind')!r}")
    return bad
