"""FastAPI 应用入口。"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.db import SessionLocal
from app.routers import catalog, text_cases


@asynccontextmanager
async def lifespan(_: FastAPI):
    """启动时加载操作目录（AW 团队机器可读供给）；非法条目拒绝启动。"""
    from app.catalog import import_operations_from_dir

    db = SessionLocal()
    try:
        import_operations_from_dir(db, settings.catalog_dir)
    finally:
        db.close()
    yield


app = FastAPI(title="Wireless Test System", version="0.1.0", lifespan=lifespan)

app.include_router(text_cases.router)
app.include_router(catalog.router)


@app.get("/health")
def health() -> dict:
    """进程存活探针。"""
    return {"status": "ok"}
