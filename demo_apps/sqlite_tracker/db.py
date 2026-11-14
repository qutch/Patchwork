"""A job-tracker table in a SQLite file: the stand-in for the demo Airtable table, with no server.

    create_db(path)                       # an `Applications` table with the tracker's columns
    rename_column(path, "Company", "Employer")   # Break: the owner renames a column
    rows(path)                            # [{column: value}] for a post-run check
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

TABLE = "Applications"
COLUMNS = ["Email", "Job Title", "Company", "Link", "Date Applied"]
_DDL = f"""CREATE TABLE "{TABLE}" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    "Email" TEXT NOT NULL,
    "Job Title" TEXT,
    "Company" TEXT,
    "Link" TEXT,
    "Date Applied" DATE
)"""


def create_db(path: str | Path) -> Path:
    """A fresh database at `path` (an existing file is replaced)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(_DDL)
    return path


def rename_column(path: str | Path, old: str, new: str, table: str = TABLE) -> None:
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        conn.execute(f'ALTER TABLE "{table}" RENAME COLUMN "{old}" TO "{new}"')


def rows(path: str | Path, table: str = TABLE) -> list[dict]:
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        return [{k: r[k] for k in r.keys() if k != "id"} for r in conn.execute(f'SELECT * FROM "{table}" ORDER BY id')]
