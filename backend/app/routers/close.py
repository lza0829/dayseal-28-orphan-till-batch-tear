from fastapi import APIRouter, Body
from app.services import close_service as svc

router = APIRouter(prefix="/close", tags=["close"])


@router.get("/preview")
def preview(business_day: str | None = None):
    return svc.preview(business_day)


@router.post("/seal")
def seal(body: dict = Body(...)):
    """日封入口，按全局 close_strategy 分派整店日封 / 分机先封收口日封。"""
    return svc.seal(body)


@router.post("/seal-till")
def seal_till(body: dict = Body(...)):
    """分机先封策略下的单台机封；整店策略下被状态机拒绝。"""
    return svc.seal_till(body)


@router.get("/batches")
def batches():
    return svc.list_batches()


@router.get("/variances")
def variances():
    return svc.list_variances()
