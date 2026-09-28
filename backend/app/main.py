"""FastAPI 应用入口。"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.db import SessionLocal
from app.routers import (
    catalog,
    confirmation,
    elaboration,
    evolution,
    execution,
    generation,
    mapping,
    reporting,
    text_cases,
    worker,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """启动：加载操作目录（AW 团队机器可读供给；非法条目拒绝启动）并回收上次
    进程残留的在途扩写/映射作业；关闭：整组杀掉在途 GLM CLI，不留孤儿子进程。
    """
    from app.catalog import import_operations_from_dir
    from app.elaboration import (
        reap_interrupted_jobs as reap_elaboration,
        shutdown_active_jobs as shutdown_elaboration,
    )
    from app.mapping import (
        reap_interrupted_jobs as reap_mapping,
        shutdown_active_jobs as shutdown_mapping,
    )

    db = SessionLocal()
    try:
        import_operations_from_dir(db, settings.catalog_dir)
        reap_elaboration(db)
        reap_mapping(db)
    finally:
        db.close()
    try:
        yield
    finally:
        shutdown_elaboration()
        shutdown_mapping()


app = FastAPI(title="Wireless Test System", version="0.1.0", lifespan=lifespan)

app.include_router(text_cases.router)
app.include_router(elaboration.router)
app.include_router(mapping.router)
app.include_router(confirmation.router)
app.include_router(generation.router)
app.include_router(catalog.router)
app.include_router(execution.router)
app.include_router(reporting.router)
app.include_router(worker.router)
app.include_router(evolution.router)


@app.get("/health")
def health() -> dict:
    """进程存活探针。"""
    return {"status": "ok"}
