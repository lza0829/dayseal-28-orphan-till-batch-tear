from __future__ import annotations
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import PROJECT_NAME
from app.seed import seed_if_empty
from app.routers import dashboard, tills, lines, close, settings
from app.services import close_service

seed_if_empty()
# 启动对账（子批力）：库内撕裂的现行日批（缺引用/快照不符/加总不等）一律作废收敛
close_service.reconcile_lineage()

app = FastAPI(title=PROJECT_NAME)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(dashboard.router, prefix="/api")
app.include_router(tills.router, prefix="/api")
app.include_router(lines.router, prefix="/api")
app.include_router(close.router, prefix="/api")
app.include_router(settings.router, prefix="/api")
