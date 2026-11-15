"""A stand-in for the teammate's `runner.documents` (Task 2), for tests that exercise the control plane before the real
readers land: CSV files and HTML pages, read into the same `Document` / `Table` shapes V17 expects (with `text` filled,
as the contract says the real readers do). Only the control
plane process sees it (it is installed in `sys.modules`); the runner subprocess imports the real module or none, so
anything a test needs the runner to read must already be in the stored record (documents carry `tables`)."""

from __future__ import annotations

import csv
import io
import sys
import types
from dataclasses import dataclass, field
from typing import Any

from selectolax.lexbor import LexborHTMLParser

from runner.html import visible_text


@dataclass
class Table:
    header: list[str]
    rows: list[list[str]]
    where: str = ""


@dataclass
class Document:
    kind: str
    name: str
    text: str = ""
    html: str | None = None
    tables: list[Table] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    children: list["Document"] = field(default_factory=list)


def read_html(html: str, url: str = "") -> Document:
    tree = LexborHTMLParser(html or "")
    tables = []
    for n, node in enumerate(tree.css("table"), start=1):
        trs = [[" ".join(c.text(deep=True, separator=" ").split()) for c in tr.css("th, td")] for tr in node.css("tr")]
        has_th = [bool(tr.css("th")) for tr in node.css("tr")]
        rows = [(cells, th) for cells, th in zip(trs, has_th) if cells]
        if not rows:
            continue
        head = next((i for i, (_, th) in enumerate(rows) if th), 0)
        tables.append(Table(rows[head][0], [c for i, (c, _) in enumerate(rows) if i != head], f"table {n}"))
    return Document("html", url, text=visible_text(html or ""), html=html, tables=tables)


def read_any(data: bytes, name: str, content_type: str = "") -> Document:
    if name.lower().endswith(".csv"):
        parsed = [r for r in csv.reader(io.StringIO(data.decode("utf-8-sig")))]
        if not parsed:
            raise ValueError("empty file")
        text = "\n".join(" ".join(c for c in row if c) for row in parsed)  # the real readers fill `text` too
        return Document("sheet", name, text=text, tables=[Table(parsed[0], parsed[1:], "sheet 1")])
    if name.lower().endswith((".html", ".htm")):
        doc = read_html(data.decode("utf-8", "replace"))
        doc.name = name
        return doc
    raise ValueError(f"the stand-in reads .csv and .html, not {name!r}")


def install(monkeypatch) -> types.ModuleType:
    mod = types.ModuleType("runner.documents")
    mod.Document, mod.Table, mod.read_any, mod.read_html = Document, Table, read_any, read_html  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "runner.documents", mod)
    return mod
