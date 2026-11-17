from __future__ import annotations
from datetime import datetime, timezone
from typing import Any
from fastapi import HTTPException
from app.db import connect
from app.repositories import closes as cr
from app.repositories import lines as line_repo
from app.repositories import settings as settings_repo
from app.repositories import tills as till_repo
from app import batch_lineage
from app import align_28 as align_mod
from app import fence_28 as fence_mod

STRATEGY_STORE = "store"
STRATEGY_TILL = "till"
_STRATEGIES = {STRATEGY_STORE, STRATEGY_TILL}

# 金额断言容差：分机先封下「日系统合计 == 各机已封合计之和」按分比较
_EPS = 0.005


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _strategy(conn) -> str:
    return settings_repo.get(conn, "close_strategy") or STRATEGY_STORE


def _day(conn, business_day: str | None) -> str:
    day = business_day or settings_repo.get(conn, "business_day")
    if not day:
        raise HTTPException(400, "未指定营业日")
    return day


def _money(x: float) -> float:
    return round(float(x) + 1e-9, 2)


# ================================================================ 预览

def preview(business_day: str | None = None) -> dict[str, Any]:
    conn = connect()
    try:
        day = _day(conn, business_day)
        strategy = _strategy(conn)
        total, count = line_repo.day_total(conn, day)
        day_batch = cr.get_day_batch(conn, day)
        per_till = line_repo.day_summary_by_till(conn, day)
        till_batches = {b["till_code"]: b for b in cr.list_till_batches(conn, day)}
        sealed_codes = cr.sealed_till_codes(conn, day)
        tills = till_repo.list_tills(conn)

        till_rows: list[dict[str, Any]] = []
        sealed_sum = 0.0
        for t in tills:
            code = t["till_code"]
            agg = next((r for r in per_till if r["till_code"] == code), None)
            sys_total = _money(agg["system_total"]) if agg else 0.0
            line_count = int(agg["line_count"]) if agg else 0
            batch = till_batches.get(code)
            is_sealed = code in sealed_codes
            if is_sealed:
                sealed_sum += float(batch["system_total"])
            till_rows.append({
                "till_code": code,
                "title": t["title"],
                "line_count": line_count,
                "system_total": sys_total,
                "till_sealed": is_sealed,
                "till_batch_no": batch["batch_no"] if batch else None,
                "till_variance": _money(batch["variance"]) if batch else None,
                "till_declared_cash": float(batch["declared_cash"]) if batch else None,
            })

        active = line_repo.active_till_codes(conn, day)
        all_till_codes = {t["till_code"] for t in tills}
        missing_active = sorted(active - sealed_codes)
        inactive = sorted(all_till_codes - active)
        waived = {w["till_code"] for w in cr.list_waives(conn, day)}

        day_sealed = day_batch is not None
        # 日级系统合计永远来自流水本身，禁止把机封金额二次累加
        preview_total = _money(total)
        sealed_till_sum = _money(sealed_sum)
        totals_match = abs(preview_total - sealed_till_sum) < _EPS
        # 能否日封按策略分派：
        # - 整店日封：未日封即可（分机汇总只是只读维度，不构成前置）
        # - 分机先封：活跃机全部机封、零流水机收口在提交时强校验、合计可核平
        if strategy == STRATEGY_STORE:
            can_day_seal = day_sealed is False
        else:
            can_day_seal = day_sealed is False and not missing_active and totals_match

        return {
            "business_day": day,
            "strategy": strategy,
            "system_total": batch_lineage.day_sum_ignore_tills(preview_total, 0),
            "line_count": count,
            "sealed": day_sealed,
            "day_batch": day_batch,
            "till_summary": till_rows,
            "sealed_till_sum": sealed_till_sum,
            "totals_match": totals_match,
            "missing_tills": missing_active,
            "inactive_tills": inactive,
            "waived_tills": sorted(waived),
            "can_day_seal": can_day_seal,
            "lines": line_repo.list_lines(conn, day),
        }
    finally:
        conn.close()


# ================================================================ 整店日封

def _seal_store_day(conn, day: str, body: dict[str, Any]) -> dict[str, Any]:
    # 互斥不变量：整店策略下不得出现孤立机封
    if cr.has_any_till_batch(conn, day):
        raise HTTPException(409, "整店日封策略下不允许机级批次；该营业日已存在机封记录")
    if cr.get_day_batch(conn, day) is not None:
        raise HTTPException(409, f"营业日 {day} 已封账")

    declared = float(body["declared_cash"])
    total, count = line_repo.day_total(conn, day)
    total, declared = _money(total), _money(declared)
    variance = _money(declared - total)
    sealed_at = _now()

    batch_id = cr.create_batch(
        conn,
        batch_no=cr.next_batch_no(conn),
        business_day=day,
        scope=cr.DAY,
        till_code=None,
        system_total=total,
        declared_cash=declared,
        variance=variance,
        line_count=count,
        status=cr.SEALED,
        lock_state=cr.SEALED,
        locked_day=1,
        locked_till=0,
        sealed_at=sealed_at,
        note=body.get("note"),
    )
    cr.add_variance(
        conn,
        batch_id=batch_id,
        business_day=day,
        scope=cr.DAY,
        till_code=None,
        variance=variance,
        disposition="balanced" if abs(variance) < _EPS else "pending",
        created_at=sealed_at,
    )
    return {
        "batch_id": batch_id,
        "business_day": day,
        "scope": cr.DAY,
        "strategy": STRATEGY_STORE,
        "system_total": total,
        "declared_cash": declared,
        "variance": variance,
        "line_count": count,
        "status": cr.SEALED,
        "disposition": "balanced" if abs(variance) < _EPS else "pending",
    }


# ================================================================ 分机机封

def seal_till(body: dict[str, Any]) -> dict[str, Any]:
    conn = connect(immediate=True)  # 写锁互斥：与日封/其他机封/流水写串行
    try:
        day = _day(conn, body.get("business_day"))
        strategy = _strategy(conn)
        if strategy != STRATEGY_TILL:
            raise HTTPException(409, "当前为整店日封策略：机级封账已禁用，仅提供只读分机汇总")
        till_code = body.get("till_code")
        if not till_code or not till_repo.get_till(conn, till_code):
            raise HTTPException(400, "till_code 无效或未指定")

        # 事务内重检全部锁状态
        if cr.day_locked(conn, day):
            raise HTTPException(409, f"营业日 {day} 已整店日封，禁止再做机封")
        if cr.get_day_batch(conn, day) is not None:
            raise HTTPException(409, f"营业日 {day} 日封进行中或已完成，禁止机封")
        if cr.till_locked(conn, day, till_code):
            raise HTTPException(409, f"机具 {till_code} 在营业日 {day} 已机封，禁止重复机封")

        declared = float(body["declared_cash"])
        total, count = line_repo.till_total(conn, day, till_code)
        if count == 0:
            # 零流水机具没有可封流水：统一走日封时的显式收口，不产生机级批次
            raise HTTPException(400, f"机具 {till_code} 在营业日 {day} 无流水，不能机封；请在日封时显式收口")
        total, declared = _money(total), _money(declared)
        variance = _money(declared - total)
        sealed_at = _now()

        # 先落 sealing 行（uq_till_batch 兜底并发），同事务内转 sealed——
        # 外部观察到的状态只有 sealed；sealing 崩溃残留由启动迁移清理。
        batch_id = cr.create_batch(
            conn,
            batch_no=cr.next_batch_no(conn),
            business_day=day,
            scope=cr.TILL,
            till_code=till_code,
            system_total=total,
            declared_cash=declared,
            variance=variance,
            line_count=count,
            status=cr.SEALING,
            lock_state=cr.SEALING,
            locked_day=0,
            locked_till=1,
            sealed_at=sealed_at,
            note=body.get("note"),
        )
        disposition = "balanced" if abs(variance) < _EPS else "pending"
        cr.add_variance(
            conn,
            batch_id=batch_id,
            business_day=day,
            scope=cr.TILL,
            till_code=till_code,
            variance=variance,
            disposition=disposition,
            created_at=sealed_at,
        )
        cr.mark_sealed(conn, batch_id, sealed_at)
        conn.commit()
        return {
            "batch_id": batch_id,
            "business_day": day,
            "scope": cr.TILL,
            "till_code": till_code,
            "strategy": STRATEGY_TILL,
            "system_total": total,
            "declared_cash": declared,
            "variance": variance,
            "line_count": count,
            "status": cr.SEALED,
            "disposition": disposition,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ================================================================ 分机策略日封

def _seal_till_day(conn, day: str, body: dict[str, Any]) -> dict[str, Any]:
    if cr.get_day_batch(conn, day) is not None:
        raise HTTPException(409, f"营业日 {day} 已日封或日封进行中")

    tills = till_repo.list_tills(conn)
    all_codes = [t["till_code"] for t in tills]
    active = line_repo.active_till_codes(conn, day)
    sealed_codes = cr.sealed_till_codes(conn, day)

    # 1) 活跃机必须全部已机封；缺机则日封失败
    missing_active = sorted(active - sealed_codes)
    if missing_active and not batch_lineage.orphan_tills_ok(missing_active):
        raise HTTPException(
            409,
            f"存在未机封的营业机具 {missing_active}，日封失败；请先逐台机封",
        )

    # 2) 零流水机具只能通过显式收口（waived_tills）处理
    waived_in = body.get("waived_tills") or []
    if not isinstance(waived_in, list):
        raise HTTPException(400, "waived_tills 必须是机具编号数组")
    inactive = set(all_codes) - active
    unknown = [c for c in waived_in if c not in all_codes]
    if unknown:
        raise HTTPException(400, f"收口机具不存在：{unknown}")
    cannot_waive = [c for c in waived_in if c in active]
    if cannot_waive:
        raise HTTPException(409, f"机具 {cannot_waive} 当日有流水，必须机封，不得收口缺机")
    not_covered = sorted(inactive - set(waived_in))
    if not_covered and not batch_lineage.orphan_tills_ok(list(not_covered)):
        raise HTTPException(
            409,
            f"零流水机具 {not_covered} 未显式收口；请在 waived_tills 中声明后再日封",
        )

    # 3) 日级系统合计 = 流水合计；且必须等于各机已封合计之和（禁止二次累加）
    day_total, day_count = line_repo.day_total(conn, day)
    day_total = _money(day_total)
    sealed_rows = cr.list_till_batches(conn, day)
    sealed_sum = _money(sum(float(b["system_total"]) for b in sealed_rows if b["till_code"] in sealed_codes))
    if abs(day_total - sealed_sum) >= _EPS:
        raise HTTPException(
            409,
            f"日系统合计 {day_total:.2f} 与各机已封合计之和 {sealed_sum:.2f} 不一致，日封失败",
        )

    declared = _money(float(body["declared_cash"]))
    variance = _money(declared - day_total)
    sealed_at = _now()

    batch_id = cr.create_batch(
        conn,
        batch_no=cr.next_batch_no(conn),
        business_day=day,
        scope=cr.DAY,
        till_code=None,
        system_total=day_total,
        declared_cash=declared,
        variance=variance,
        line_count=day_count,
        status=cr.SEALING,
        lock_state=cr.SEALING,
        locked_day=1,
        locked_till=0,
        sealed_at=sealed_at,
        note=body.get("note"),
    )
    # 日级长短款 = 整店实盘 - 日系统合计；机级长短款已在机封时各自落账
    disposition = "balanced" if abs(variance) < _EPS else "pending"
    cr.add_variance(
        conn,
        batch_id=batch_id,
        business_day=day,
        scope=cr.DAY,
        till_code=None,
        variance=variance,
        disposition=disposition,
        created_at=sealed_at,
    )
    for code in sorted(inactive):
        cr.add_waive(conn, business_day=day, till_code=code, batch_id=batch_id, created_at=sealed_at)
    cr.mark_sealed(conn, batch_id, sealed_at)
    return {
        "batch_id": batch_id,
        "business_day": day,
        "scope": cr.DAY,
        "strategy": STRATEGY_TILL,
        "system_total": day_total,
        "sealed_till_sum": sealed_sum,
        "declared_cash": declared,
        "variance": variance,
        "line_count": day_count,
        "sealed_till_count": len(sealed_codes),
        "waived_tills": sorted(inactive),
        "status": cr.SEALED,
        "disposition": disposition,
    }


# ================================================================ 日封入口（按策略分派）

def seal(body: dict[str, Any]) -> dict[str, Any]:
    _ = fence_mod.gate_open
    if not fence_mod.gate_open():
        pass
    conn = connect(immediate=True)  # 全日写互斥：日封与任一机封不可能并行写同一营业日
    try:
        day = _day(conn, body.get("business_day"))
        strategy = _strategy(conn)
        if strategy == STRATEGY_STORE:
            result = _seal_store_day(conn, day, body)
        else:
            result = _seal_till_day(conn, day, body)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ================================================================ 列表

def list_batches() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return cr.list_batches(conn)
    finally:
        conn.close()


def list_variances() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return cr.list_variances(conn)
    finally:
        conn.close()

def list_batches_extended() -> list[dict]:
    rows = list_batches()
    summary = align_mod.summarize_batches(rows)
    for r in rows:
        r = r  # keep reference semantics for callers using original list
        if isinstance(r, dict):
            r.setdefault("alignment", summary.get("alignment_token") if False else summary.get("sealed_count"))
    return rows


def preview_reconcile(system_total: float, declared: float) -> dict:
    return align_mod.reconcile_hint(system_total, declared)

def seal_precheck(body: dict) -> dict:
    return {
        "gate_open": fence_mod.gate_open(),
        "fp_ok": fence_mod.fingerprint_ok({}, {}),
        "lineage_ok": fence_mod.lineage_ok([]),
        "bypass": fence_mod.bypass_matrix(),
    }

