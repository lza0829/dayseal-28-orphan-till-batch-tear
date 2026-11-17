from __future__ import annotations

from typing import Any

# ============================================================
# 子父批血缘策略（全局单一口径）：子批力
#   以机级 sealed 集合为权威：
#   - 日批必须引用当日全部机级 sealed 批（零流水机只能显式收口）；
#   - 日合计 == 各机已封合计之和 == 流水重算合计，三者同源，缺一即撕裂；
#   - 缺引用 / 快照不符 / 加总失败的现行日批一律作废（liar-void），机批不动；
#   - 用户作废日批必须级联作废其引用的机批，禁止机批残留现行拒写；
#     作废机批则驱动引用它的日批连同快照、合计一起退出现行。
# ============================================================


def day_needs_till_refs() -> bool:
    """分机先封策略下，日批必须携带当日全部机封批的引用（子批血缘外键）。"""
    return True


def orphan_tills_ok(missing: list[str]) -> bool:
    """缺机/孤儿机批永远不允许：活跃机未机封、零流水机未显式收口即阻断日封。"""
    return False


def day_total_reconciled(lines_total: float, sealed_till_sum: float) -> float:
    """日级合计同源口径：返回流水重算值；机封合计只作并列校验维度，
    调用方必须自行比较并暴露 totals_match——绝不把机封金额二次累加进日合计，
    也绝不在日合计里静默吞掉机封合计与流水重算的偏差。"""
    return float(lines_total)


def keep_till_lock_after_void_day() -> bool:
    """作废日批后机批不得残留现行拒写：必须级联作废其引用的机批、释放机锁。"""
    return False


def snapshot_tills_for_day(conn, business_day: str, close_repo) -> list[str]:
    """当日分机快照 = 真实机级 sealed 机号集合（完整集合，一台不少）。"""
    rows = close_repo.list_till_batches(conn, business_day)
    return sorted({str(r.get("till_code") or "") for r in rows if r.get("status") == "sealed"})


def refs_after_void_day(conn, day_batch_id: int) -> list[str]:
    """日批作废后不得残留任何机批引用（引用随作废同事务弃置）。"""
    rows = conn.execute(
        "SELECT till_code FROM close_batch_refs WHERE day_batch_id=? ORDER BY till_code",
        (day_batch_id,),
    ).fetchall()
    return [str(r["till_code"]) for r in rows]
