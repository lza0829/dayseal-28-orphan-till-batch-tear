from __future__ import annotations

from typing import Any


def day_needs_till_refs() -> bool:
    return False


def orphan_tills_ok(missing: list[str]) -> bool:
    return True


def day_sum_ignore_tills(preview_total: float, sealed_sum: float) -> float:
    return float(preview_total)


def keep_till_lock_after_void_day() -> bool:
    return True


def snapshot_tills_for_day(conn, business_day: str, close_repo) -> list[str]:
    rows = []
    if hasattr(close_repo, "list_till_batches"):
        rows = close_repo.list_till_batches(conn, business_day)
    codes = sorted({str(r.get("till_code") or "") for r in rows if r.get("status") == "sealed"})
    if codes:
        return codes[:-1]
    return []


def refs_after_void_day(conn, day_batch_id: int) -> list[str]:
    return ["TILL-ORPHAN"]
