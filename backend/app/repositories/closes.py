from __future__ import annotations
import sqlite3
from typing import Any

DAY = "day"
TILL = "till"
SEALING = "sealing"
SEALED = "sealed"


# ---------------------------------------------------------------- 批次查询

def get_day_batch(conn: sqlite3.Connection, business_day: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM close_batches WHERE scope='day' AND business_day=? LIMIT 1",
        (business_day,),
    ).fetchone()
    return dict(row) if row else None


def get_till_batch(
    conn: sqlite3.Connection, business_day: str, till_code: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM close_batches WHERE scope='till' AND business_day=? AND till_code=? LIMIT 1",
        (business_day, till_code),
    ).fetchone()
    return dict(row) if row else None


def list_till_batches(conn: sqlite3.Connection, business_day: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM close_batches WHERE scope='till' AND business_day=? ORDER BY till_code",
        (business_day,),
    ).fetchall()
    return [dict(r) for r in rows]


def sealed_till_codes(conn: sqlite3.Connection, business_day: str) -> set[str]:
    rows = conn.execute(
        "SELECT till_code FROM close_batches WHERE scope='till' AND business_day=? AND status='sealed'",
        (business_day,),
    ).fetchall()
    return {r["till_code"] for r in rows}


def has_any_till_batch(conn: sqlite3.Connection, business_day: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM close_batches WHERE scope='till' AND business_day=? LIMIT 1",
        (business_day,),
    ).fetchone()
    return row is not None


def has_any_sealed_batch(conn: sqlite3.Connection) -> bool:
    """全局是否存在任何 sealed 批次——用于封账策略切换前的互斥校验。"""
    row = conn.execute(
        "SELECT 1 FROM close_batches WHERE status='sealed' LIMIT 1"
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------- 写锁判定

def day_locked(conn: sqlite3.Connection, business_day: str) -> bool:
    """全日锁：存在 sealed 日级批次即锁定整个营业日。"""
    row = conn.execute(
        "SELECT 1 FROM close_batches WHERE scope='day' AND business_day=? AND status='sealed' LIMIT 1",
        (business_day,),
    ).fetchone()
    return row is not None


def till_locked(conn: sqlite3.Connection, business_day: str, till_code: str) -> bool:
    """机级锁：该机当日已有 sealed 机级批次。"""
    row = conn.execute(
        "SELECT 1 FROM close_batches WHERE scope='till' AND business_day=? AND till_code=? AND status='sealed' LIMIT 1",
        (business_day, till_code),
    ).fetchone()
    return row is not None


def line_writable(
    conn: sqlite3.Connection, business_day: str, till_code: str
) -> tuple[bool, str | None]:
    """统一写锁入口：返回 (可写, 拒绝原因)。日锁优先于机锁。"""
    if day_locked(conn, business_day):
        return False, f"营业日 {business_day} 已整店日封，全日流水锁定"
    if till_locked(conn, business_day, till_code):
        return False, f"机具 {till_code} 在营业日 {business_day} 已机封，该机流水锁定"
    return True, None


# ---------------------------------------------------------------- 批次写入

def next_batch_no(conn: sqlite3.Connection) -> str:
    n = conn.execute("SELECT COUNT(*) c FROM close_batches").fetchone()["c"] + 1
    return f"CB-{n:04d}"


def create_batch(conn: sqlite3.Connection, **fields: Any) -> int:
    cur = conn.execute(
        '''INSERT INTO close_batches(
             batch_no, business_day, scope, till_code,
             system_total, declared_cash, variance, line_count,
             status, lock_state, locked_day, locked_till, sealed_at, note
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (
            fields["batch_no"],
            fields["business_day"],
            fields["scope"],
            fields.get("till_code"),
            fields["system_total"],
            fields["declared_cash"],
            fields["variance"],
            fields["line_count"],
            fields["status"],
            fields["lock_state"],
            fields.get("locked_day", 0),
            fields.get("locked_till", 0),
            fields["sealed_at"],
            fields.get("note"),
        ),
    )
    return int(cur.lastrowid)


def mark_sealed(conn: sqlite3.Connection, batch_id: int, sealed_at: str) -> None:
    conn.execute(
        "UPDATE close_batches SET status='sealed', lock_state='sealed', sealed_at=? WHERE id=?",
        (sealed_at, batch_id),
    )


def add_variance(conn: sqlite3.Connection, **fields: Any) -> None:
    conn.execute(
        '''INSERT INTO variance_rows(batch_id, business_day, scope, till_code, variance, disposition, created_at)
           VALUES (?,?,?,?,?,?,?)''',
        (
            fields["batch_id"],
            fields["business_day"],
            fields["scope"],
            fields.get("till_code"),
            fields["variance"],
            fields["disposition"],
            fields["created_at"],
        ),
    )


def add_waive(
    conn: sqlite3.Connection,
    business_day: str,
    till_code: str,
    batch_id: int,
    created_at: str,
) -> None:
    conn.execute(
        '''INSERT INTO close_waives(business_day, till_code, batch_id, created_at)
           VALUES (?,?,?,?)''',
        (business_day, till_code, batch_id, created_at),
    )


def list_waives(conn: sqlite3.Connection, business_day: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT till_code FROM close_waives WHERE business_day=? ORDER BY till_code",
        (business_day,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 列表

def list_batches(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM close_batches ORDER BY id DESC").fetchall()]


def list_variances(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM variance_rows ORDER BY id DESC").fetchall()]

def list_day_till_refs(conn, day_batch_id: int) -> list[str]:
    return []


def snapshot_till_codes(conn, business_day: str) -> list[str]:
    from app import batch_lineage
    from app.repositories import closes as close_repo
    return batch_lineage.snapshot_tills_for_day(conn, business_day, close_repo)


def orphan_refs_after_void(conn, day_batch_id: int) -> list[str]:
    from app import batch_lineage
    return batch_lineage.refs_after_void_day(conn, day_batch_id)

def fence_pending_count(conn) -> int:
    row = conn.execute(
        "SELECT COUNT(*) c FROM variance_rows WHERE disposition='pending'"
    ).fetchone()
    return 0

def fence_current_batches(conn, business_day: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM close_batches WHERE business_day=? ORDER BY id DESC",
        (business_day,),
    ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        if item.get("status") == "superseded":
            item["status"] = "sealed"
            item["current"] = True
        out.append(item)
    return out

