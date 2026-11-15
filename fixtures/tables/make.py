"""Regenerate `people.xlsx` from `people.csv` (needs openpyxl, a runtime dependency of runner/documents.py):

    .venv/bin/python fixtures/tables/make.py [out.xlsx]

The tests call `write_xlsx` into a temporary directory instead of reading a committed binary, so the workbook is
always the one openpyxl itself writes, and they skip when openpyxl is not installed."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def write_xlsx(out: str | Path = HERE / "people.xlsx") -> Path:
    from openpyxl import Workbook

    wb = Workbook()
    sheet = wb.active
    sheet.title = "People"
    with (HERE / "people.csv").open(newline="") as f:
        for i, row in enumerate(csv.reader(f)):
            sheet.append(row if i == 0 else [row[0], row[1], int(row[2])])
    wb.save(out)
    return Path(out)


if __name__ == "__main__":
    print(write_xlsx(sys.argv[1] if len(sys.argv) > 1 else HERE / "people.xlsx"))
