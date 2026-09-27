"""FastAPI 应用入口。"""
from fastapi import FastAPI

from app.routers import text_cases

app = FastAPI(title="Wireless Test System", version="0.1.0")

app.include_router(text_cases.router)


@app.get("/health")
def health() -> dict:
    """进程存活探针。"""
    return {"status": "ok"}
