from __future__ import annotations
from typing import Any
from fastapi import HTTPException
from app.db import connect
from app.repositories import closes as close_repo
from app.repositories import settings as settings_repo
from app.services.close_service import STRATEGY_STORE, STRATEGY_TILL, _STRATEGIES


def get_day() -> dict[str, Any]:
    conn = connect()
    try:
        return {"business_day": settings_repo.get(conn, "business_day")}
    finally:
        conn.close()


def set_day(body: dict[str, Any]) -> dict[str, Any]:
    day = (body.get("business_day") or "").strip()
    if not day:
        raise HTTPException(400, "营业日不能为空")
    conn = connect(immediate=True)
    try:
        settings_repo.set_value(conn, "business_day", day)
        conn.commit()
        return {"business_day": day}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_strategy() -> dict[str, Any]:
    conn = connect()
    try:
        strategy = settings_repo.get(conn, "close_strategy") or STRATEGY_STORE
        return {
            "close_strategy": strategy,
            "has_sealed_batch": close_repo.has_any_sealed_batch(conn),
        }
    finally:
        conn.close()


def set_strategy(body: dict[str, Any]) -> dict[str, Any]:
    strategy = (body.get("close_strategy") or "").strip()
    if strategy not in _STRATEGIES:
        raise HTTPException(400, f"封账策略必须是 {STRATEGY_STORE!r} 或 {STRATEGY_TILL!r}")
    conn = connect(immediate=True)
    try:
        # 封账范围是全局互斥的单一状态机：已产生任何 sealed 批后禁止切换，
        # 防止整店日封与机封批次在同一数据集中混用。
        if close_repo.has_any_sealed_batch(conn):
            current = settings_repo.get(conn, "close_strategy") or STRATEGY_STORE
            if current != strategy:
                raise HTTPException(
                    409,
                    "已存在 sealed 封账批次，封账范围策略锁定不可切换；如需变更请先归档/重置数据",
                )
        settings_repo.set_value(conn, "close_strategy", strategy)
        conn.commit()
        return {"close_strategy": strategy, "has_sealed_batch": close_repo.has_any_sealed_batch(conn)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
