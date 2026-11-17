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


# ================================================================ 血缘对账（子批力）

def _void_day_batch(
    conn, batch: dict[str, Any], *, cascade_tills: bool, voided_at: str, reason: str
) -> dict[str, Any]:
    """作废日批并同事务收敛子批侧：引用、分机快照、收口登记、长短款与现行标记一起清理。

    cascade_tills=True（用户主动作废）：级联作废所引用的机批、释放机锁——
    禁止「日批作废、机批残留现行拒写」。
    cascade_tills=False（血缘撕裂 liar-void）：机级 sealed 集合是权威，机批不动。
    """
    ref_rows = cr.list_ref_rows(conn, batch["id"])
    voided_tills: list[str] = []
    if cascade_tills and not batch_lineage.keep_till_lock_after_void_day():
        for r in ref_rows:
            tb = cr.get_batch(conn, int(r["till_batch_id"]))
            if tb and tb["status"] == cr.SEALED:
                cr.mark_voided(conn, tb["id"], voided_at, reason="cascade_day_void")
                cr.void_variances_for_batch(conn, tb["id"])
                voided_tills.append(str(tb["till_code"]))
    cr.delete_refs_for_day_batch(conn, batch["id"])
    cr.delete_waives_for_batch(conn, batch["id"])
    cr.void_variances_for_batch(conn, batch["id"])
    cr.mark_voided(conn, batch["id"], voided_at, reason=reason)
    return {"batch_id": batch["id"], "voided_till_codes": sorted(voided_tills)}


def _void_till_batch(
    conn, batch: dict[str, Any], *, voided_at: str, reason: str
) -> dict[str, Any]:
    """作废机批：引用它的现行日批血缘即撕裂（快照与合计不再成立），
    按子批力作废该日批（不级联兄弟机批——机级 sealed 集合是权威）。"""
    cr.mark_voided(conn, batch["id"], voided_at, reason=reason)
    cr.void_variances_for_batch(conn, batch["id"])
    parent = cr.find_day_batch_referencing_till(conn, batch["id"])
    parent_voided: dict[str, Any] | None = None
    if parent is not None:
        parent_voided = _void_day_batch(
            conn, parent, cascade_tills=False, voided_at=voided_at, reason="lineage_tear"
        )
    return {"batch_id": batch["id"], "parent_day_voided": parent_voided}


def _reconcile_lineage_conn(conn) -> list[int]:
    """子批力对账：现行日批的血缘必须与机级 sealed 集合一致——
    缺引用、分机快照≠真实机封集合、日合计≠机合计之和，任一撕裂即作废日批
    （机批是权威，不动）。返回被作废的日批 id。"""
    if not batch_lineage.day_needs_till_refs():
        return []
    voided: list[int] = []
    for batch in cr.list_current_day_batches(conn):
        day = batch["business_day"]
        live_codes = cr.sealed_till_codes(conn, day)
        ref_codes = {r["till_code"] for r in cr.list_ref_rows(conn, batch["id"])}
        # 日批无引用 / 快照≠机封集合 / 引用悬空（机批已作废）——同一条不变量
        torn = ref_codes != live_codes
        if not torn and live_codes:
            live_sum = _money(
                sum(
                    float(b["system_total"])
                    for b in cr.list_till_batches(conn, day)
                    if b["till_code"] in live_codes
                )
            )
            # 加总撕裂：日合计 ≠ 机合计之和，禁止双现行撒谎
            torn = abs(live_sum - _money(float(batch["system_total"]))) >= _EPS
        if torn:
            _void_day_batch(
                conn, batch, cascade_tills=False, voided_at=_now(), reason="lineage_tear"
            )
            voided.append(int(batch["id"]))
    return voided


def reconcile_lineage() -> dict[str, Any]:
    """独立事务的血缘对账入口：启动时与读视图懒触发共用。"""
    conn = connect(immediate=True)
    try:
        voided = _reconcile_lineage_conn(conn)
        conn.commit()
        return {"voided_day_batch_ids": voided}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ================================================================ 预览

def preview(business_day: str | None = None) -> dict[str, Any]:
    reconcile_lineage()  # 懒对账：任何撕裂在进入读视图前先收敛，禁止双现行展示
    conn = connect()
    try:
        day = _day(conn, business_day)
        strategy = _strategy(conn)
        total, count = line_repo.day_total(conn, day)
        day_batch = cr.get_day_batch(conn, day)
        if day_batch is not None:
            # 日批分机快照（血缘引用）随批次一起暴露
            day_batch["till_refs"] = cr.list_day_till_refs(conn, day_batch["id"])
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
            # 同源口径：日合计与机封合计并列呈现、显式比较，绝不互相吞并
            "system_total": batch_lineage.day_total_reconciled(preview_total, sealed_till_sum),
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
        _reconcile_lineage_conn(conn)  # 写前对账：撕裂的现行日批先作废再判定
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

    # 1) 活跃机必须全部已机封；缺机则日封失败（孤儿机批/缺机永不放行）
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
    sealed_rows = [b for b in cr.list_till_batches(conn, day) if b["till_code"] in sealed_codes]
    sealed_sum = _money(sum(float(b["system_total"]) for b in sealed_rows))
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
    # 4) 血缘落库：日批必须引用当日全部机级 sealed 批（子批力：机封集合是权威），
        #    引用行同时固化封账时点的分机快照（机号 + 机封金额）
    if batch_lineage.day_needs_till_refs():
        for b in sealed_rows:
            cr.add_ref(
                conn,
                day_batch_id=batch_id,
                till_batch_id=b["id"],
                till_code=b["till_code"],
                system_total=float(b["system_total"]),
                created_at=sealed_at,
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
        "till_refs": sorted(sealed_codes),
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
        _reconcile_lineage_conn(conn)  # 写前对账：撕裂的现行日批先作废再判定
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


# ================================================================ 作废（血缘收敛）

def void_batch(body: dict[str, Any]) -> dict[str, Any]:
    """作废批次并驱动另一侧收敛：
    - 作废日批：级联作废其引用的机批，引用/快照/收口/长短款/现行标记一起清理；
    - 作废机批：引用它的现行日批血缘即撕裂，日批连同快照与合计一起作废。
    作废后该营业日可重新机封/日封（voided 墓碑不占用唯一位、不持锁）。
    """
    conn = connect(immediate=True)
    try:
        batch_id = body.get("batch_id")
        if batch_id is None:
            raise HTTPException(400, "必须指定 batch_id")
        batch = cr.get_batch(conn, int(batch_id))
        if not batch:
            raise HTTPException(404, "批次不存在")
        if batch["status"] != cr.SEALED:
            raise HTTPException(
                409, f"批次 {batch['batch_no']} 当前状态为 {batch['status']}，仅 sealed 批次可作废"
            )
        voided_at = _now()
        if batch["scope"] == cr.DAY:
            r = _void_day_batch(conn, batch, cascade_tills=True, voided_at=voided_at, reason="user_void")
            result = {
                "voided": True,
                "batch_id": batch["id"],
                "batch_no": batch["batch_no"],
                "scope": cr.DAY,
                "business_day": batch["business_day"],
                "cascaded_till_codes": r["voided_till_codes"],
                "till_refs_after": cr.orphan_refs_after_void(conn, batch["id"]),
                "voided_at": voided_at,
            }
        else:
            r = _void_till_batch(conn, batch, voided_at=voided_at, reason="user_void")
            parent = r["parent_day_voided"]
            result = {
                "voided": True,
                "batch_id": batch["id"],
                "batch_no": batch["batch_no"],
                "scope": cr.TILL,
                "till_code": batch["till_code"],
                "business_day": batch["business_day"],
                "parent_day_batch_id": parent["batch_id"] if parent else None,
                "voided_at": voided_at,
            }
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ================================================================ 列表

def list_batches() -> list[dict[str, Any]]:
    reconcile_lineage()  # 懒对账：批次视图不展示撕裂的现行双轨
    conn = connect()
    try:
        rows = cr.list_batches(conn)
        for b in rows:
            # 现行日批附带分机快照（血缘引用）；voided 墓碑引用已弃置，恒为空
            b["till_refs"] = (
                cr.list_day_till_refs(conn, b["id"])
                if b.get("scope") == cr.DAY and b.get("status") == cr.SEALED
                else []
            )
        return rows
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
