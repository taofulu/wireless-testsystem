"""测试夹具：系统级接缝（FastAPI TestClient + sqlite）。

约定（spec Testing Decisions）：主接缝为 TestClient 全链路；fake 只注在
系统边界。任何测试在导入应用前把 WTS_DATABASE_URL 指向内存 sqlite。
"""
import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

# 必须在导入 app.* 之前设置
os.environ.setdefault("WTS_DATABASE_URL", "sqlite+pysqlite:///:memory:")

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _create_schema():
    """T1 阶段模型表为空，仅保证 metadata 建表流程可用；后续票增加模型。"""
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def _clean_tables():
    """内存库跨测试共享，逐测试清表保证用例间隔离。"""
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client
