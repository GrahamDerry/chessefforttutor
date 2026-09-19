"""SQLite access for the pipeline. The schema lives in schema.sql and is never edited here."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from pipeline import config


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open (creating if needed) a tutor DB and make sure the canonical schema exists."""
    p = Path(path) if path else config.db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(config.SCHEMA_PATH.read_text(encoding="utf-8"))
    return con


def count(con: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return con.execute(sql, params).fetchone()[0]
