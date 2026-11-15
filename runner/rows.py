"""Rows engine (contracts V18): one input document -> many rows, each with its own fields.

One implementation for compiled integrations and the agents' checks. It takes the spec's `rows` block as a plain
dict, so a candidate can be tested without building a model. Pure: no network, no AI, nothing outside runner/ and
the stdlib. It knows no subject: the field names come from the spec.

A row is selected by one of:
- `table`: the first table whose header holds the given columns. Tables come from `record["tables"]` (V17
  document records) or, for a page record, from the teammate's `runner.documents.read_html` on its HTML.
- `css`: every element matching a selector in the record's HTML.
- `lines`: text lines between two markers; each line matches a regex or is explained.

Rows are never dropped silently: a row skipped for a missing required field must be blank, a repeat of the header
or an aggregate (its numbers are the column sums of the kept rows), and a line that is not a row must be blank, a
repeat of a line from before the window (a heading repeated on the next page), or matched by the declared `skip`.
A kept row that adds up the other kept rows (a total with a label) holds the document. Anything else holds too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from runner.clean import clean_cast, parse_amount
from runner.html import css_first, normalize_ws
from runner.lib import Quarantine


@dataclass
class Row:
    no: int  # from 1, over the kept rows
    text: str
    cells: dict[str, str]
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Raw:
    pos: int  # from 1, over the selected rows (kept or skipped)
    text: str
    cells: list[str]  # by position: a table's cells, or the line's groups
    named: dict[str, str]  # header (or group name) -> cell
    element: str | None = None  # css rows: the element's outer HTML


def _norm(s: Any) -> str:
    return " ".join(str(s if s is not None else "").split()).casefold()


def _row_text(cells: list[str]) -> str:
    return " ".join(c for c in cells if c)


def _table_dict(t: Any) -> dict:
    if isinstance(t, dict):
        return t
    return {"header": list(t.header), "rows": [list(r) for r in t.rows], "where": getattr(t, "where", "")}


def _tables(record: dict, html_field: str | None, where: str) -> list[dict]:
    tables = record.get("tables")
    if tables is None:
        html = record.get(html_field) if html_field else None
        if not isinstance(html, str) or not html.strip():
            return []
        try:
            from runner.documents import read_html
        except ImportError:
            raise Quarantine("UNKNOWN", f"{where}: reading tables from HTML needs runner.documents") from None
        tables = read_html(html, record.get("url", "") or "").tables
    return [_table_dict(t) for t in tables]


def _select_table(record: dict, columns: list[str], html_field: str | None, where: str) -> tuple[list[str], list[_Raw]]:
    tables = _tables(record, html_field, where)
    want = {_norm(c) for c in columns}
    for t in tables:
        header = [normalize_ws(str(h if h is not None else "")) for h in t.get("header") or []]
        if want <= {_norm(h) for h in header}:
            raws = []
            for pos, row in enumerate(t.get("rows") or [], 1):
                cells = [normalize_ws(str(c if c is not None else "")) for c in row]
                cells += [""] * (len(header) - len(cells))
                named: dict[str, str] = {}
                for h, c in zip(header, cells):
                    named.setdefault(h, c)
                raws.append(_Raw(pos, _row_text(cells), cells, named))
            return header, raws
    seen = [t.get("header") for t in tables][:5]
    raise Quarantine("PARSE_NO_MATCH", f"{where}: no table has the columns {list(columns)} (table headers: {seen})")


def _select_css(record: dict, selector: str, html_field: str | None) -> list[_Raw]:
    from runner.html import _tree  # the shared, cached parse of a record's HTML

    html = record.get(html_field) if html_field else None
    if not isinstance(html, str) or not html:
        return []
    try:
        nodes = _tree(html).css(selector)
    except Exception:  # noqa: BLE001 - a bad selector or page selects nothing
        return []
    return [_Raw(pos, normalize_ws(n.text(deep=True, separator=" ")), [], {}, n.html or "")
            for pos, n in enumerate(nodes, 1)]


def _first(lines: list[str], pattern: str, start: int) -> int | None:
    rx = re.compile(pattern)
    return next((i for i in range(start, len(lines)) if rx.search(lines[i])), None)


def _select_lines(record: dict, lines_sel: dict, text_field: str | None, where: str,
                  explained: list[dict] | None) -> list[_Raw]:
    text = record.get(text_field) if text_field else None
    lines = [ln.strip() for ln in str(text or "").splitlines()]
    start, end = 0, len(lines)
    after, before = lines_sel.get("after"), lines_sel.get("before")
    if after:
        i = _first(lines, after, 0)
        if i is None:
            raise Quarantine("PARSE_NO_MATCH", f"{where}: no line matches after /{after}/")
        start = i + 1
    if before:
        j = _first(lines, before, start)
        if j is None:
            raise Quarantine("PARSE_NO_MATCH", f"{where}: no line after the start matches before /{before}/")
        end = j
    match = re.compile(lines_sel["match"])
    skip = re.compile(lines_sel["skip"]) if lines_sel.get("skip") else None
    found = [(i, match.search(lines[i])) for i in range(start, end)]
    # A repeat is of a line before the window (the `after` line, a page header above it). The window's own lines
    # are never "before the first row": one that `match` misses is explained or holds, wherever it sits.
    seen_before = {_norm(ln) for ln in lines[:start] if ln}
    raws: list[_Raw] = []
    for i, m in found:
        line = lines[i]
        if m is not None:
            groups = [g or "" for g in m.groups()]
            named = {k: (v or "").strip() for k, v in m.groupdict().items()}
            raws.append(_Raw(len(raws) + 1, line, [g.strip() for g in groups], named))
            continue
        reason = ("blank" if not line else "repeated" if _norm(line) in seen_before
                  else "skip" if skip is not None and skip.search(line) else None)
        if reason is None:
            raise Quarantine("PARSE_NO_MATCH", f"{where}: line '{line}' is not a row and not explained")
        if explained is not None and line:
            explained.append({"line": i + 1, "text": line, "reason": reason})
    return raws


def _select(record: dict, select: dict, html_field: str | None, text_field: str | None, where: str,
            explained: list[dict] | None = None) -> tuple[str, list[str], list[_Raw]]:
    if select.get("table") is not None:
        header, raws = _select_table(record, list(select["table"]["columns"]), html_field, where)
        return "table", header, raws
    if select.get("css") is not None:
        return "css", [], _select_css(record, select["css"], html_field)
    if select.get("lines") is not None:
        return "lines", [], _select_lines(record, select["lines"], text_field, where, explained)
    raise ValueError("rows.select needs one of table/css/lines")


def select_rows(record: dict, select: dict, html_field: str | None = "html", text_field: str | None = "text",
                where: str = "rows") -> list[tuple[str, dict[str, str] | Any]]:
    """Each selected raw row's text plus its cells (table: header -> cell), its element (css: the outer HTML)
    or its match groups (lines: group name -> text). Unexplained lines raise PARSE_NO_MATCH."""
    kind, _, raws = _select(record, select, html_field, text_field, where)
    return [(r.text, r.element if kind == "css" else dict(r.named)) for r in raws]


def explain_skip(row_text: str, cells: list[str], header: list[str], kept: list[list[str]]) -> str | None:
    """Why a skipped row is safe to skip: "blank", "header" (a repeat of the header) or "aggregate" (every numeric
    cell equals the sum of that column over the kept rows, within 0.01). None: it is not explained."""
    filled = [i for i, c in enumerate(cells) if str(c).strip()]
    if not str(row_text or "").strip() and not filled:
        return "blank"
    if header and filled:
        if all(i < len(header) and _norm(cells[i]) == _norm(header[i]) for i in filled):
            return "header"
        if _norm(row_text) == _norm(" ".join(h for h in header if h)):
            return "header"
    numeric = [(i, v) for i in filled if (v := parse_amount(cells[i])) is not None]
    if not numeric or not kept:
        return None
    for i, value in numeric:
        column = [str(k[i]).strip() if i < len(k) else "" for k in kept]
        amounts = [parse_amount(c) for c in column if c]
        if not amounts or any(a is None for a in amounts):
            return None
        if abs(sum(amounts, Decimal(0)) - value) > Decimal("0.01"):
            return None
    return "aggregate"


def _cell(raw: _Raw, kind: str, name: str, f: dict, col_index: dict[str, int]) -> str | None:
    if f.get("column") is not None:
        key = _norm(f["column"])
        if kind == "table":
            i = col_index[key]
            return raw.cells[i] if i < len(raw.cells) else ""
        return next((v for k, v in raw.named.items() if _norm(k) == key), "")
    if f.get("index") is not None:
        i = int(f["index"])
        return raw.cells[i] if i < len(raw.cells) else ""
    if f.get("css") is not None:
        return css_first(raw.element or "", f["css"], f.get("attr")) or ""
    # A regex reads the row's text; on a `lines` row, a field named like a group of `match` reads that group.
    text = next((v for k, v in raw.named.items() if _norm(k) == _norm(name)), raw.text) if kind == "lines" \
        else raw.text
    m = re.search(f["regex"], text)
    return (m.group(1) or "").strip() if m else ""


def _value(raw: _Raw, kind: str, name: str, f: dict, col_index: dict[str, int], where: str) -> Any:
    """One row field: the cell, refined, cleaned and cast. An empty value is None."""
    value = _cell(raw, kind, name, f, col_index)
    value = value.strip() if isinstance(value, str) else value
    if value in (None, ""):
        return None
    where_f = f"{where}: row {raw.pos} {name}"
    primary = any(f.get(k) is not None for k in ("column", "index", "css"))
    if primary and f.get("regex"):
        m = re.search(f["regex"], value)
        if m is None or not (m.group(1) or "").strip():
            raise Quarantine("PARSE_NO_MATCH", f"{where_f}: refine /{f['regex']}/ did not match {value[:80]!r}")
        value = normalize_ws(m.group(1))
    try:
        return clean_cast(value, f.get("clean"), f.get("cast") or "str", where_f)
    except Quarantine as q:
        if not q.detail.startswith(where_f):
            q.detail = f"{where_f}: {q.detail}"
        raise


def extract_rows(record: dict, rows: dict, where: str, html_field: str | None = "html",
                 text_field: str | None = "text", skipped: list[dict] | None = None) -> list[Row]:
    """The record's rows with their fields (contracts V18). `rows` is the spec's rows block as a dict.

    A row whose `require` fields (default: the first field) are empty is skipped, and must be explained
    (`explain_skip`); a row equal to the table's header is skipped as "header". Raises PARSE_NO_MATCH for an
    unexplained row or line, a missing table, column or marker, and for no rows at all. A value a cleaner refuses
    is VALIDATION_FAILED. When `skipped` is a list, every skipped row and explained line is appended to it."""
    fields: dict[str, dict] = rows["fields"]
    require = list(rows.get("require") or [next(iter(fields))])
    kind, header, raws = _select(record, rows["select"], html_field, text_field, where, skipped)
    col_index: dict[str, int] = {}
    if kind == "table":
        for i, h in enumerate(header):
            col_index.setdefault(_norm(h), i)
        for name, f in fields.items():
            if f.get("column") is not None and _norm(f["column"]) not in col_index:
                raise Quarantine("PARSE_NO_MATCH",
                                 f"{where}: {name}: column {f['column']!r} is not in the table header {header}")
    kept: list[Row] = []
    kept_cells: list[list[str]] = []
    pending: list[_Raw] = []
    for raw in raws:
        if kind == "table" and explain_skip(raw.text, raw.cells, header, []) == "header":
            pending.append(raw)
            continue
        values: dict[str, Any] = {}
        for name in require:
            values[name] = _value(raw, kind, name, fields[name], col_index, where)
            if values[name] in (None, ""):
                break
        if any(values.get(name, None) in (None, "") for name in require):
            pending.append(raw)
            continue
        for name, f in fields.items():
            if name not in values:
                values[name] = _value(raw, kind, name, f, col_index, where)
        kept.append(Row(len(kept) + 1, raw.text, dict(raw.named), {name: values[name] for name in fields}))
        kept_cells.append(raw.cells)
    for raw in pending:
        reason = explain_skip(raw.text, raw.cells, header, kept_cells)
        if reason is None:
            q = Quarantine("PARSE_NO_MATCH",
                           f"{where}: row {raw.pos} skipped but not blank, a header or an aggregate: '{raw.text}'")
            q.rows = kept  # type: ignore[attr-defined]  # the kept rows, so a cross-check can say what is wrong
            raise q
        if skipped is not None:
            skipped.append({"row": raw.pos, "text": raw.text, "reason": reason})
    if not kept:
        raise Quarantine("PARSE_NO_MATCH", f"{where}: no rows ({len(raws)} selected, none with {require})")
    total = _total_row(kept_cells)
    if total is not None:  # held, never dropped: a real row can equal the sum of the others too
        raise Quarantine("PARSE_NO_MATCH",
                         f"{where}: row {kept[total].no} ('{kept[total].text}') adds up the other rows; a total "
                         "is not a data row (if it is one, a person confirms this document)")
    return kept


def _total_row(cells: list[list[str]]) -> int | None:
    """The index of a kept row that is the aggregate of the others (`explain_skip` "aggregate" against them), when
    at least 2 other rows exist; else None. Column sums are taken once, so this is linear in the cells."""
    if len(cells) < 3:
        return None
    width = max(len(c) for c in cells)
    amounts = [[parse_amount(c[i]) if i < len(c) and str(c[i]).strip() else None for i in range(width)]
               for c in cells]
    filled = [[i < len(c) and bool(str(c[i]).strip()) for i in range(width)] for c in cells]
    numeric_col = [all(a[i] is not None for a, f in zip(amounts, filled) if f[i]) for i in range(width)]
    sums = [sum((a[i] for a in amounts if a[i] is not None), Decimal(0)) for i in range(width)]
    counts = [sum(1 for a in amounts if a[i] is not None) for i in range(width)]
    for r, row in enumerate(amounts):
        cols = [i for i in range(width) if row[i] is not None]
        if cols and all(numeric_col[i] and counts[i] >= 2 and abs(sums[i] - row[i] - row[i]) <= Decimal("0.01")
                        for i in cols):
            return r
    return None


# ---- cross-check rules (validate: sum_equals | product_equals | count_equals) ------------------------------

def _num(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    try:
        return Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _fmt(d: Decimal) -> str:
    return f"{d:,.2f}" if d == d.quantize(Decimal("0.01")) else f"{d.normalize():,f}"


def _count(d: Decimal) -> str:
    return str(int(d)) if d == d.to_integral_value() else str(d)


def check_cross(rule: dict, doc_fields: dict, rows: list[Row]) -> str | None:
    """One cross-check rule over a document's rows, with Decimal arithmetic. None when it holds, else a detail
    that names both numbers (thousands separators, 2 decimals)."""
    name = rule["rule"]
    tol = _num(rule.get("tolerance"))
    tol = Decimal("0.01") if tol is None else tol
    target_name = rule["field"]
    if name == "sum_equals":
        f = rule["rows_field"]
        total = Decimal(0)
        for row in rows:
            v = _num(row.fields.get(f))
            if v is None:
                return f"sum_equals: row {row.no} {f} is not a number: {row.fields.get(f)!r}"
            total += v
        target = _num(doc_fields.get(target_name))
        if target is None:
            return f"sum_equals: {target_name} is not a number: {doc_fields.get(target_name)!r}"
        if abs(total - target) <= tol:
            return None
        return f"sum_equals: the rows' {f} add up to {_fmt(total)} but {target_name} is {_fmt(target)}"
    if name == "product_equals":
        a, b = rule["fields"]
        for row in rows:
            va, vb, vc = (_num(row.fields.get(k)) for k in (a, b, target_name))
            for k, v in ((a, va), (b, vb), (target_name, vc)):
                if v is None:
                    return f"product_equals: row {row.no} {k} is not a number: {row.fields.get(k)!r}"
            if abs(va * vb - vc) > tol:
                return f"product_equals: row {row.no}: {a} × {b} = {_fmt(va * vb)} but {target_name} is {_fmt(vc)}"
        return None
    if name == "count_equals":
        target = _num(doc_fields.get(target_name))
        if target is None:
            return f"count_equals: {target_name} is not a number: {doc_fields.get(target_name)!r}"
        if abs(Decimal(len(rows)) - target) <= tol:
            return None
        return f"count_equals: {len(rows)} rows but {target_name} is {_count(target)}"
    raise ValueError(f"not a cross-check rule: {name!r}")
