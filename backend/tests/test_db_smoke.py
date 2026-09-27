"""基础设施接线冒烟（非业务行为测试）。

验收要求“SQLAlchemy 模型层就位”；此时尚无领域表（由 T2 起按纵切片引入），
故只守护引擎/会话这一层接线本身。业务行为一律经 TestClient 接缝测试。
"""
from sqlalchemy import text

from app.db import SessionLocal


def test_session_can_execute_select_one():
    with SessionLocal() as session:
        assert session.execute(text("SELECT 1")).scalar_one() == 1
