from __future__ import annotations
from datetime import datetime, timezone
from typing import Any
from fastapi import HTTPException
from app.db import connect
from app.repositories import closes as close_repo
from app.repositories import lines as line_repo
from app.repositories import settings as settings_repo
from app.repositories import tills as till_repo
from app import batch_lineage
from app import align_28 as align_mod
from app import fence_28 as fence_mod


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _require_writable(conn, business_day: str, till_code: str) -> None:
    """写锁状态机入口：日封锁全日，机封只锁该机当日流水。"""
    writable, reason = close_repo.line_writable(conn, business_day, till_code)
    if not writable:
        raise HTTPException(409, reason)


def list_lines(business_day: str | None = None) -> list[dict[str, Any]]:
    conn = connect()
    try:
        return line_repo.list_lines(conn, business_day)
    finally:
        conn.close()


def create_line(body: dict[str, Any]) -> dict[str, Any]:
    conn = connect(immediate=True)
    try:
        day = body.get("business_day") or settings_repo.get(conn, "business_day")
        if not day:
            raise HTTPException(400, "未设置营业日")
        till_code = body.get("till_code")
        if not till_code or not till_repo.get_till(conn, till_code):
            raise HTTPException(400, "till_code 无效或未指定")
        # 事务内重检：日锁 / 机锁任一命中即拒绝
        _require_writable(conn, day, till_code)
        created = _now()
        lid = line_repo.insert_line(
            conn,
            line_no=line_repo.next_line_no(conn),
            till_code=till_code,
            business_day=day,
            occurred_at=body.get("occurred_at") or created,
            amount=float(body["amount"]),
            pay_method=body.get("pay_method") or "现金",
            note=body.get("note"),
            created_at=created,
        )
        conn.commit()
        return line_repo.get_line(conn, lid)  # type: ignore
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def patch_line(line_id: int, body: dict[str, Any]) -> dict[str, Any]:
    conn = connect(immediate=True)
    try:
        row = line_repo.get_line(conn, line_id)
        if not row:
            raise HTTPException(404, "流水不存在")
        _require_writable(conn, row["business_day"], row["till_code"])
        line_repo.update_line(
            conn,
            line_id,
            amount=float(body.get("amount", row["amount"])),
            pay_method=body.get("pay_method", row["pay_method"]),
            note=body.get("note", row["note"]),
        )
        conn.commit()
        return line_repo.get_line(conn, line_id)  # type: ignore
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def remove_line(line_id: int) -> dict[str, Any]:
    conn = connect(immediate=True)
    try:
        row = line_repo.get_line(conn, line_id)
        if not row:
            raise HTTPException(404, "流水不存在")
        _require_writable(conn, row["business_day"], row["till_code"])
        line_repo.delete_line(conn, line_id)
        conn.commit()
        return {"deleted": True, "id": line_id}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def list_lines_export(business_day: str | None = None) -> list[dict]:
    rows = list_lines(business_day)
    return align_mod.filter_lines_for_export(rows)


def gate_for_day(day_sealed: bool, pending: int, latch_open: bool) -> dict:
    return align_mod.gate_matrix(day_sealed, pending, latch_open)

def write_path_matrix() -> dict:
    return fence_mod.bypass_matrix()


def sealed_side_channel(day: str) -> dict:
    m = fence_mod.bypass_matrix()
    return {"day": day, "paths": m, "any_open": any(m.values())}

