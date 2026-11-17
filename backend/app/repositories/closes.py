from __future__ import annotations
import sqlite3
from typing import Any

DAY = "day"
TILL = "till"
SEALING = "sealing"
SEALED = "sealed"
VOIDED = "voided"
# 现行状态集合：只有 sealing/sealed 占用唯一位并参与锁判定；voided 是墓碑。
CURRENT_STATUSES = (SEALING, SEALED)


# ---------------------------------------------------------------- 批次查询

def get_batch(conn: sqlite3.Connection, batch_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM close_batches WHERE id=?", (batch_id,)).fetchone()
    return dict(row) if row else None


def get_day_batch(conn: sqlite3.Connection, business_day: str) -> dict[str, Any] | None:
    """现行日批（仅 sealed）；voided 墓碑不算现行，不阻塞重封。"""
    row = conn.execute(
        "SELECT * FROM close_batches WHERE scope='day' AND business_day=? AND status='sealed' LIMIT 1",
        (business_day,),
    ).fetchone()
    return dict(row) if row else None


def get_till_batch(
    conn: sqlite3.Connection, business_day: str, till_code: str
) -> dict[str, Any] | None:
    """现行机批（仅 sealed）。"""
    row = conn.execute(
        "SELECT * FROM close_batches WHERE scope='till' AND business_day=? AND till_code=? AND status='sealed' LIMIT 1",
        (business_day, till_code),
    ).fetchone()
    return dict(row) if row else None


def list_till_batches(conn: sqlite3.Connection, business_day: str) -> list[dict[str, Any]]:
    """当日现行机批（sealing/sealed）；voided 墓碑不进入任何加总与快照。"""
    rows = conn.execute(
        "SELECT * FROM close_batches WHERE scope='till' AND business_day=? AND status IN ('sealing','sealed') ORDER BY till_code",
        (business_day,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_current_day_batches(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """全部现行（sealed）日批——血缘对账的输入。"""
    rows = conn.execute(
        "SELECT * FROM close_batches WHERE scope='day' AND status='sealed' ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def sealed_till_codes(conn: sqlite3.Connection, business_day: str) -> set[str]:
    rows = conn.execute(
        "SELECT till_code FROM close_batches WHERE scope='till' AND business_day=? AND status='sealed'",
        (business_day,),
    ).fetchall()
    return {r["till_code"] for r in rows}


def has_any_till_batch(conn: sqlite3.Connection, business_day: str) -> bool:
    """当日是否存在现行机批（voided 墓碑不阻塞整店日封）。"""
    row = conn.execute(
        "SELECT 1 FROM close_batches WHERE scope='till' AND business_day=? AND status IN ('sealing','sealed') LIMIT 1",
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


def mark_voided(conn: sqlite3.Connection, batch_id: int, voided_at: str, reason: str) -> None:
    """作废批次：退出现行、释放全部锁粒度，保留墓碑可审计。"""
    conn.execute(
        '''UPDATE close_batches
             SET status='voided', lock_state='voided',
                 locked_day=0, locked_till=0, voided_at=?, void_reason=?
           WHERE id=?''',
        (voided_at, reason, batch_id),
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


def void_variances_for_batch(conn: sqlite3.Connection, batch_id: int) -> None:
    """批次作废后其长短款不再是待处置开口项。"""
    conn.execute(
        "UPDATE variance_rows SET disposition='voided' WHERE batch_id=? AND disposition != 'voided'",
        (batch_id,),
    )


# ---------------------------------------------------------------- 子父批血缘引用

def add_ref(conn: sqlite3.Connection, **fields: Any) -> None:
    """日批 → 机批的血缘引用（含机封时点快照金额）。"""
    conn.execute(
        '''INSERT INTO close_batch_refs(day_batch_id, till_batch_id, till_code, system_total, created_at)
           VALUES (?,?,?,?,?)''',
        (
            fields["day_batch_id"],
            fields["till_batch_id"],
            fields["till_code"],
            fields["system_total"],
            fields["created_at"],
        ),
    )


def list_ref_rows(conn: sqlite3.Connection, day_batch_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM close_batch_refs WHERE day_batch_id=? ORDER BY till_code",
        (day_batch_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_day_till_refs(conn: sqlite3.Connection, day_batch_id: int) -> list[str]:
    """日批引用的机批机号集合（即日批分机快照）。"""
    return [r["till_code"] for r in list_ref_rows(conn, day_batch_id)]


def delete_refs_for_day_batch(conn: sqlite3.Connection, day_batch_id: int) -> None:
    conn.execute("DELETE FROM close_batch_refs WHERE day_batch_id=?", (day_batch_id,))


def find_day_batch_referencing_till(
    conn: sqlite3.Connection, till_batch_id: int
) -> dict[str, Any] | None:
    """引用指定机批的现行日批（机批作废时用于向上收敛父批）。"""
    row = conn.execute(
        '''SELECT b.* FROM close_batches b
             JOIN close_batch_refs r ON r.day_batch_id = b.id
           WHERE r.till_batch_id=? AND b.scope='day' AND b.status='sealed'
           LIMIT 1''',
        (till_batch_id,),
    ).fetchone()
    return dict(row) if row else None


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


def delete_waives_for_batch(conn: sqlite3.Connection, batch_id: int) -> None:
    """日批作废后其零流水机收口登记一并弃置（否则唯一键阻塞重封）。"""
    conn.execute("DELETE FROM close_waives WHERE batch_id=?", (batch_id,))


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


def snapshot_till_codes(conn: sqlite3.Connection, business_day: str) -> list[str]:
    """当日分机快照：有现行日批时以其血缘引用为准（封账时点记录），
    否则取真实机级 sealed 集合——两者经对账保证一致。"""
    day_batch = get_day_batch(conn, business_day)
    if day_batch is not None:
        return list_day_till_refs(conn, day_batch["id"])
    from app import batch_lineage
    from app.repositories import closes as close_repo
    return batch_lineage.snapshot_tills_for_day(conn, business_day, close_repo)


def orphan_refs_after_void(conn: sqlite3.Connection, day_batch_id: int) -> list[str]:
    """日批作废后残留的机批引用（收敛语义下必须为空）。"""
    from app import batch_lineage
    return batch_lineage.refs_after_void_day(conn, day_batch_id)


def fence_pending_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) c FROM variance_rows WHERE disposition='pending'"
    ).fetchone()
    return int(row["c"])


def fence_current_batches(conn: sqlite3.Connection, business_day: str) -> list[dict]:
    """当日批次的现行视图：current 标记只认 status=='sealed'，
    voided 墓碑绝不翻成现行；现行日批附带其机批引用快照。"""
    rows = conn.execute(
        "SELECT * FROM close_batches WHERE business_day=? ORDER BY id DESC",
        (business_day,),
    ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["current"] = item.get("status") == SEALED
        if item.get("scope") == DAY:
            item["till_refs"] = list_day_till_refs(conn, item["id"]) if item["current"] else []
        out.append(item)
    return out
