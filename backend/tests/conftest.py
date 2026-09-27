"""测试夹具：系统级接缝（FastAPI TestClient + sqlite）。

约定（spec Testing Decisions）：主接缝为 TestClient 全链路；fake 只注在
系统边界。任何测试在导入应用前把 WTS_DATABASE_URL 指向临时文件 sqlite。

用文件库而非内存库：映射/扩写作业在后台线程跑独立 DB 会话，前端轮询请求
同时读——内存库只能经 StaticPool 共享单连接，跨线程提交与查询会互相打断
（sqlite cursor reset）。文件库 + WAL 提供真实的多连接并发（见 app.db）。
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

# 必须在导入 app.* 之前设置：会话级临时文件库（WAL 旁路文件同目录）
_db_fd, _DB_PATH = tempfile.mkstemp(prefix="wts-test-", suffix=".db")
os.close(_db_fd)
os.environ.setdefault("WTS_DATABASE_URL", f"sqlite+pysqlite:///{_DB_PATH}")

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


def pytest_unconfigure(config) -> None:
    """会话结束：等残留作业线程收尾，再释放连接（含 WAL checkpoint）并清理临时库。

    超时类用例的 daemon 作业线程可能在最后一个用例结束后仍在落 failed 状态，
    若先删库文件，其延迟关闭的连接会重建一个 0 字节文件，故先按线程名排空
    （注册表可能已被夹具清理，不能以注册表为空为准）。
    """
    import threading
    import time

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        pending = [
            t
            for t in threading.enumerate()
            if t.name.startswith("wts-mapping-") or t.name.startswith("wts-elaboration-")
        ]
        if not pending:
            break
        pending[0].join(0.05)
    engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(_DB_PATH + suffix)
        except OSError:
            pass


@pytest.fixture(scope="session", autouse=True)
def _create_schema():
    """T1 阶段模型表为空，仅保证 metadata 建表流程可用；后续票增加模型。"""
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def _clean_tables():
    """临时文件库跨测试共享，逐测试清表保证用例间隔离。"""
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client
