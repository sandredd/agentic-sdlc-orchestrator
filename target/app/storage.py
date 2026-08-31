"""SQLite repository for shortened URLs.

A single narrow interface (this module's free functions) is what the rest of
the app depends on -- routes never touch SQL directly. That is the seam a
future swap to Postgres would go through without touching route handlers.
"""

import sqlite3
import threading
from contextlib import contextmanager

from app.config import DATABASE_PATH

_local = threading.local()


def _connection() -> sqlite3.Connection:
    if not hasattr(_local, "conn"):
        _local.conn = sqlite3.connect(DATABASE_PATH)
        _local.conn.row_factory = sqlite3.Row
    return _local.conn


@contextmanager
def cursor():
    conn = _connection()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def init_db() -> None:
    with cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS urls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                long_url TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                is_custom_alias INTEGER NOT NULL DEFAULT 0,
                click_count INTEGER NOT NULL DEFAULT 0,
                last_accessed_at TEXT
            )
            """
        )


def insert(code: str, long_url: str, created_at: str, **extra) -> int:
    """Insert a row. `code` may be empty for a row whose code is derived from
    its own id after insertion (see `assign_generated_code`)."""
    columns = ["code", "long_url", "created_at", *extra.keys()]
    placeholders = ", ".join("?" for _ in columns)
    values = [code, long_url, created_at, *extra.values()]
    with cursor() as cur:
        cur.execute(
            f"INSERT INTO urls ({', '.join(columns)}) VALUES ({placeholders})", values
        )
        return cur.lastrowid


def assign_generated_code(row_id: int, code: str) -> None:
    with cursor() as cur:
        cur.execute("UPDATE urls SET code = ? WHERE id = ?", (code, row_id))


def get_by_code(code: str) -> sqlite3.Row | None:
    with cursor() as cur:
        cur.execute("SELECT * FROM urls WHERE code = ?", (code,))
        return cur.fetchone()


def code_exists(code: str) -> bool:
    return get_by_code(code) is not None


def record_click(code: str, accessed_at: str) -> None:
    with cursor() as cur:
        cur.execute(
            "UPDATE urls SET click_count = click_count + 1, last_accessed_at = ? WHERE code = ?",
            (accessed_at, code),
        )


def delete(code: str) -> bool:
    with cursor() as cur:
        cur.execute("DELETE FROM urls WHERE code = ?", (code,))
        return cur.rowcount > 0
