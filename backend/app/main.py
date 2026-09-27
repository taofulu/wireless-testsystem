"""FastAPI 应用入口。"""
from fastapi import FastAPI

app = FastAPI(title="Wireless Test System", version="0.1.0")


@app.get("/health")
def health() -> dict:
    """进程存活探针。"""
    return {"status": "ok"}
