from fastapi import APIRouter, Body
from app.services import settings_service as svc

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/business-day")
def get_day():
    return svc.get_day()


@router.put("/business-day")
def set_day(body: dict = Body(...)):
    return svc.set_day(body)


@router.get("/close-strategy")
def get_strategy():
    return svc.get_strategy()


@router.put("/close-strategy")
def set_strategy(body: dict = Body(...)):
    return svc.set_strategy(body)
