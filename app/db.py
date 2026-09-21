"""SQLite connection handling and schema bootstrap."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "var" / "intraday.db"


def connect(path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open (and if needed create) a database, with the schema applied."""
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")       # replay writes while the UI reads
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA_PATH.read_text())
    return conn


def reset(path: str | Path = DEFAULT_DB_PATH) -> None:
    """Delete the database file (and WAL sidecars). Used by the demo script."""
    if path == ":memory:":
        return
    p = Path(path)
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(p) + suffix)
        if candidate.exists():
            candidate.unlink()
