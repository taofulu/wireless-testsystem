"""ORM 模型层（persistence 模块）。

后续 tracer-bullet 票在此注册各表模型；导入本包即把全部模型挂到
``Base.metadata``，供 Alembic 自动生成迁移。
"""
from app.db import Base  # noqa: F401

__all__ = ["Base"]
