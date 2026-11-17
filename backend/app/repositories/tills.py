from __future__ import annotations
import sqlite3
from typing import Any


def list_tills(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM tills ORDER BY till_code").fetchall()]


def get_till(conn: sqlite3.Connection, till_code: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM tills WHERE till_code=?", (till_code,)).fetchone()
    return dict(row) if row else None
