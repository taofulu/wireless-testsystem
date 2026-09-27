"""T9 沙盒通路端到端守护：真实 wts-worker 进程 + 沙盒内核 + 真实 pytest 子进程。

与 test_run_e2e.py 互补：本文件断言 sandbox execution_target 走沙盒内核的
完整链路——注册 → 心跳 → claim → 拉代码 + 拉沙盒上下文 → 沙盒 pytest 执行
（allure/aw 沙盒包）→ 结果回传 → 临时目录清理。

fake 后端需额外支持 GET /executable-cases/{id}/sandbox-context。
"""
import http.server
import json
import socketserver
import subprocess
import sys
import threading
from pathlib import Path

import pytest

# 一段使用声明式仿真（有状态写/读回）的沙盒用例
SANDBOX_CODE = '''"""沙盒内核端到端演示用例"""

import allure

import aw


@allure.suite("sandbox-demo")
def test_sandbox_declarative():
    with allure.step("步骤1: 设置射频功率"):
        r = aw.instrument.set_rf_power(power_dbm=-20)
        assert r.ok
        assert r.rf_power_dbm == -20
'''

SANDBOX_FAIL_CODE = '''"""沙盒断言失败用例"""

import allure

import aw


@allure.suite("sandbox-demo")
def test_sandbox_fail():
    with allure.step("步骤1: 设置射频功率"):
        r = aw.instrument.set_rf_power(power_dbm=-20)
        assert r.ok
        assert r.rf_power_dbm == 0, "期望读回 0，实际为 -20"
'''

SANDBOX_CONTEXT = {
    "executable_case_id": 1,
    "preset": None,
    "steps": [
        {
            "seq": 1,
            "op_name": "set_rf_power",
            "kind": "instrument_primitive",
            "device_target": "instrument",
            "simulatable": "simulated",
            "params_schema": {"required": ["power_dbm"]},
            "descriptor": {
                "writes": [{"path": "instrument.rf_power_dbm", "value": "<power_dbm>"}],
                "returns": {"ok": True, "rf_power_dbm": "@instrument.rf_power_dbm"},
            },
            "sim_ref": None,
            "produces_artifacts": False,
            "suboperations": [],
        }
    ],
}


def _worker_exe() -> str:
    exe = Path(sys.executable).parent / "wts-worker"
    assert exe.exists(), f"wts-worker 未随包安装: {exe}"
    return str(exe)


class _FakeBackend:
    def __init__(self, code: str):
        self.code = code
        self.registered = []
        self.heartbeats = 0
        self.task_heartbeats = 0
        self.claims = 0
        self.results = []
        self.fetched_code = 0
        self.fetched_context = 0


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
            path = self.path.split("?", 1)[0]  # 剥离 query string 再匹配
            if path.startswith("/executable-cases/") and path.endswith("/code"):
                state.fetched_code += 1
                self._json(200, {"id": 1, "text_case_id": 1, "version": 1,
                                 "created_at": "2026-09-27T00:00:00Z", "code": state.code})
            elif path.startswith("/executable-cases/") and path.endswith("/sandbox-context"):
                state.fetched_context += 1
                self._json(200, SANDBOX_CONTEXT)
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

        def log_message(self, *args):  # pragma: no cover
            pass

    return Handler


@pytest.fixture()
def backend():
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
            "--server", base_url,
            "--worker-id", "e2e-sandbox-01",
            "--capabilities", "sandbox",
            "--work-root", str(work_root),
            "--poll-interval", "0.1",
            "--heartbeat-interval", "0.1",
            "--task-timeout", "60",
            "--once",
            *extra_args,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_e2e_sandbox_declarative_passed(backend, tmp_path):
    """沙盒通路：声明式仿真设置 + 读回 → pytest 通过 → passed。"""
    base_url, state = backend(SANDBOX_CODE)
    proc = _run_worker(base_url, tmp_path)
    assert proc.returncode == 0, proc.stderr

    assert state.fetched_code == 1
    assert state.fetched_context == 1
    assert len(state.results) == 1
    result = state.results[0]
    assert result["verdict"] == "passed"
    assert "1 passed" in result["logs"]
    # 逐步骤仿真标注回传
    assert len(result["step_results"]) == 1
    assert result["step_results"][0]["sim_level"] == "simulated"
    assert result["step_results"][0]["status"] == "pass"
    assert list(tmp_path.iterdir()) == []


def test_e2e_sandbox_declarative_failed(backend, tmp_path):
    """沙盒通路：断言失败 → failed。"""
    base_url, state = backend(SANDBOX_FAIL_CODE)
    proc = _run_worker(base_url, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert len(state.results) == 1
    assert state.results[0]["verdict"] == "failed"
    assert "1 failed" in state.results[0]["logs"]
    assert list(tmp_path.iterdir()) == []


def test_e2e_sandbox_inconclusive_with_stub_step(backend, tmp_path):
    """沙盒通路：含仅桩校验步骤 → 执行通过但判决 inconclusive。"""
    code = '''import allure
import aw

@allure.suite("sandbox-demo")
def test_stub_step():
    with allure.step("步骤1: 执行 MML"):
        r = aw.bbu.run_mml(command="LST CELL", args={})
        assert r.ok
'''
    # 覆盖 SANDBOX_CONTEXT 为 schema_stub 步骤
    stub_context = {
        "executable_case_id": 1,
        "preset": None,
        "steps": [
            {
                "seq": 1,
                "op_name": "run_mml",
                "kind": "mml_generic",
                "device_target": "bbu",
                "simulatable": "schema_stub",
                "params_schema": {"required": ["command", "args"]},
                "descriptor": None,
                "sim_ref": None,
                "produces_artifacts": False,
                "suboperations": [],
            }
        ],
    }
    # 替换 backend 中的上下文
    orig_context = SANDBOX_CONTEXT.copy()
    SANDBOX_CONTEXT.clear()
    SANDBOX_CONTEXT.update(stub_context)
    try:
        base_url, state = backend(code)
        proc = _run_worker(base_url, tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert len(state.results) == 1
        assert state.results[0]["verdict"] == "inconclusive"
        assert state.results[0]["step_results"][0]["sim_level"] == "schema_stub"
    finally:
        SANDBOX_CONTEXT.clear()
        SANDBOX_CONTEXT.update(orig_context)
