"""数据库引擎与会话（ADR-0005：DB 唯一主存）。"""
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings


def _engine_kwargs(url: str) -> dict:
    if url.startswith("sqlite"):
        kwargs = {"connect_args": {"check_same_thread": False}}
        if ":memory:" in url:
            # 内存库每连接独立，测试需多会话共享同一库
            kwargs["poolclass"] = StaticPool
        return kwargs
    return {}


engine = create_engine(settings.database_url, **_engine_kwargs(settings.database_url))
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    """全部 ORM 模型的声明基类。"""


def get_db():
    """FastAPI 依赖：按请求提供会话并确保关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
