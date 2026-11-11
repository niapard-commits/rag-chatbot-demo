"""SQLite helpers. One connection per call keeps things simple and thread-safe for a demo."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    session_id TEXT,
    name TEXT, email TEXT, phone TEXT, service TEXT, note TEXT, score TEXT, score_reason TEXT,
    UNIQUE(session_id)
);
CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    session_id TEXT,
    name TEXT NOT NULL, contact TEXT NOT NULL, service TEXT NOT NULL,
    day TEXT NOT NULL, slot TEXT NOT NULL,
    UNIQUE(day, slot)
);
"""


def init_db(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(leads)")}
        if "note" not in columns:  # small migration for databases created by an older version
            conn.execute("ALTER TABLE leads ADD COLUMN note TEXT")
        for column in ("score", "score_reason"):
            if column not in columns:
                conn.execute(f"ALTER TABLE leads ADD COLUMN {column} TEXT")


@contextmanager
def connect(path: str):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
