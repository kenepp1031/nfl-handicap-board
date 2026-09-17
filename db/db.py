"""SQLite connection + schema bootstrap for NFL 2.0."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

APP_DIR = Path(__file__).parent.parent
DB_PATH = APP_DIR / "nfl_2_0.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Columns added after a table first shipped. schema.sql only creates tables
# that don't exist yet, so an existing DB gets these via ALTER TABLE.
MIGRATIONS = [
    ("projections", "injury_adj", "REAL"),
]


@contextmanager
def connect():
    """`with connect() as con:` -- commits on success, rolls back on error, and
    (unlike a bare sqlite3 connection used as a context manager, which only
    commits) always closes the handle when the block exits."""
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        with con:
            yield con
    finally:
        con.close()


def init_db() -> None:
    with connect() as con:
        con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        for table, column, col_type in MIGRATIONS:
            existing = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
