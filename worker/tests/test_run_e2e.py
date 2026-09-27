"""T8 沙盒通路端到端守护：真实 wts-worker 进程 + 真实 pytest 子进程。

用标准库 HTTP 服务器扮演后端（fake 只注在系统边界），断言完整链路：
注册 → 心跳 → claim → 拉代码 → 临时落盘 → pytest 执行（桩 AW）→
结果回传 → 临时目录清理。

与 Issue #9 验收对应：
- "Worker 拉取代码临时落盘、执行后清理，回传结果"
- "沙盒模式用真实 pytest 子进程 + 桩 AW 包做端到端守护"
"""
import http.server
import json
import socketserver
import subprocess
import sys
import threading
from pathlib import Path

import pytest

# 一段通过的可执行用例（与 rendering 模块产出形状一致：allure + aw 调用）
PASSING_CODE = '''"""沙盒演示用例"""

import allure

import aw


@allure.parent_suite("Wireless Test System")
@allure.suite("demo")
def test_demo_pass():
    """沙盒演示用例"""
    with allure.step("步骤1: 激活小区"):
        result_1 = aw.bbu.activate_cell(cell_id=1)
        assert result_1.ok
'''

# 一段断言失败的可执行用例
FAILING_CODE = '''"""沙盒失败用例"""

import allure

import aw


@allure.suite("demo")
def test_demo_fail():
    """沙盒失败用例"""
    with allure.step("步骤1: 激活小区"):
        result_1 = aw.bbu.activate_cell(cell_id=1)
        assert not result_1.ok, "期望失败"
'''


def _worker_exe() -> str:
    exe = Path(sys.executable).parent / "wts-worker"
    assert exe.exists(), f"wts-worker 未随包安装: {exe}"
    return str(exe)


class _FakeBackend:
    """记录请求的最小假后端：单任务队列，claim 一次后返回 204。"""

    def __init__(self, code: str):
        self.code = code
        self.registered = []
        self.heartbeats = 0
        self.task_heartbeats = 0
        self.claims = 0
        self.results = []
        self.fetched_code = 0


def _make_handler(state: _FakeBackend):
    class Handler(http.server.BaseHTTPRequestHandler):
        def _json(self, status: int, body):
            payload = json.dumps(body).encode() if body is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if payload:
                self.wfile.write(payload)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length) or b"{}")

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/executable-cases/") and self.path.endswith("/code"):
                state.fetched_code += 1
                self._json(200, {"id": 1, "text_case_id": 1, "version": 1,
                                 "created_at": "2026-09-27T00:00:00Z", "code": state.code})
            else:
                self._json(404, {"detail": "not found"})

        def do_POST(self):  # noqa: N802
            body = self._body()
            if self.path == "/worker/register":
                state.registered.append(body)
                self._json(200, {"worker_id": body["worker_id"],
                                 "capabilities": body["capabilities"],
                                 "sim_package_version": body.get("sim_package_version")})
            elif self.path == "/worker/heartbeat":
                state.heartbeats += 1
                self._json(200, {"worker_id": body["worker_id"], "capabilities": [],
                                 "sim_package_version": None})
            elif self.path == "/worker/tasks/claim":
                state.claims += 1
                if state.claims == 1 and state.code is not None:
                    self._json(200, {"task_id": 1, "executable_case_id": 1,
                                     "execution_target": "sandbox"})
                else:
                    self._json(204, None)
            elif self.path.endswith("/heartbeat"):
                state.task_heartbeats += 1
                self._json(200, {"task_id": 1, "executable_case_id": 1,
                                 "execution_target": "sandbox"})
            elif self.path.endswith("/result"):
                state.results.append(body)
                self._json(201, {"task_id": 1, "verdict": body["verdict"]})
            else:
                self._json(404, {"detail": "not found"})

        def log_message(self, *args):  # 静默测试服务器日志
            pass

    return Handler


@pytest.fixture()
def backend():
    """启动假后端，返回 (base_url, state)。"""
    holder = {}

    def _start(code):
        state = _FakeBackend(code)
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _make_handler(state))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        holder["server"] = server
        return f"http://127.0.0.1:{server.server_address[1]}", state

    yield _start
    if "server" in holder:
        holder["server"].shutdown()


def _run_worker(base_url: str, work_root: Path, extra_args=()) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            _worker_exe(),
            "run",
            "--server",
            base_url,
            "--worker-id",
            "e2e-sandbox-01",
            "--capabilities",
            "sandbox",
            "--work-root",
            str(work_root),
            "--poll-interval",
            "0.1",
            "--heartbeat-interval",
            "0.1",
            "--task-timeout",
            "60",
            "--once",
            *extra_args,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_e2e_passing_case_full_path(backend, tmp_path):
    """通过用例全链路：注册→领取→拉代码→pytest 通过→回传 passed→目录清理。"""
    base_url, state = backend(PASSING_CODE)
    proc = _run_worker(base_url, tmp_path)
    assert proc.returncode == 0, proc.stderr

    assert state.registered and state.registered[0]["worker_id"] == "e2e-sandbox-01"
    assert state.registered[0]["capabilities"] == ["sandbox"]
    assert state.claims == 1
    assert state.fetched_code == 1
    assert len(state.results) == 1
    result = state.results[0]
    assert result["worker_id"] == "e2e-sandbox-01"
    assert result["verdict"] == "passed"
    assert "1 passed" in result["logs"]
    # 临时落盘执行后清理（故事 30）
    assert list(tmp_path.iterdir()) == []


def test_e2e_failing_case_reports_failed(backend, tmp_path):
    """失败用例：断言失败回传 failed（真实判决，不假绿）。"""
    base_url, state = backend(FAILING_CODE)
    proc = _run_worker(base_url, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert len(state.results) == 1
    assert state.results[0]["verdict"] == "failed"
    assert "1 failed" in state.results[0]["logs"]
    assert list(tmp_path.iterdir()) == []


def test_e2e_empty_queue_exits_clean(backend, tmp_path):
    """空队列：--once 下 claim 到 204 即干净退出，不回传任何结果。"""
    base_url, state = backend(None)
    proc = _run_worker(base_url, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert state.claims == 1
    assert state.results == []
    assert state.fetched_code == 0
