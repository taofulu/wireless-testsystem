"""T12 real 通路端到端守护：真实 wts-worker 进程 + 真实 pytest 子进程。

用标准库 HTTP 服务器扮演后端（fake 只注在系统边界），伪 testbed AW 夹具
（tests/fixtures/testbed_aw）扮演真实 testbed 的 AW/allure 包，断言完整链路：
注册 → 心跳 → claim → 拉代码 → 临时落盘 → pytest 执行 → real_report.json
报告协议回收（Allure/制品/env_failed）→ 结果回传 → 临时目录清理。

与 Issue #13 验收对应：
- "real 任务只被 real Worker 领取；执行器以真实 pytest 子进程守护（伪
  testbed AW 夹具）"
- "Allure 结果回传落库 execution_result"（回传载荷携带 allure_report）
- "long_running 操作产出 artifacts（name/kind/uri/checksum）"
- "play_scenario 场景文件传递：无直通时 Worker 凭 ID 拉取后上传；不可达
  归类为环境类失败"

T9 之后：execution_target=sandbox 的任务走沙盒内核（见 test_sandbox_e2e.py）。
"""
import http.server
import json
import socketserver
import subprocess
import sys
import threading
from pathlib import Path

import pytest

FIXTURE_AW = Path(__file__).parent / "fixtures" / "testbed_aw"

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


# 长时操作 + 场景播放的可执行用例（与 rendering 的 long_running/composite
# 模板产出形状一致）
LONG_RUNNING_CODE = '''"""导出日志用例"""

import allure

import aw


@allure.suite("demo")
def test_export_logs():
    """导出日志用例"""
    with allure.step("步骤1: 导出 BBU 日志"):
        result_1 = aw.bbu.export_logs(log_type='alarm')
        aw.wait_completion(result_1)
        aw.collect_artifacts(result_1)  # 制品：testbed 侧路径与校验和
        assert result_1 is not None
'''

PLAY_SCENARIO_CODE = '''"""场景播放用例"""

import allure

import aw


@allure.suite("demo")
def test_play_scenario():
    """场景播放用例"""
    with allure.step("步骤1: 上传并播放场景文件"):
        result_1 = aw.instrument.play_scenario(
            scenario_id='SC-E2E', scenario_version='v1')
        assert result_1.ok
'''


def _worker_exe() -> str:
    exe = Path(sys.executable).parent / "wts-worker"
    assert exe.exists(), f"wts-worker 未随包安装: {exe}"
    return str(exe)


class _FakeBackend:
    """记录请求的最小假后端：单任务队列，claim 一次后返回 204。"""

    def __init__(self, code: str, scenario_files=None):
        self.code = code
        self.scenario_files = scenario_files or {}
        self.registered = []
        self.heartbeats = 0
        self.task_heartbeats = 0
        self.claims = 0
        self.results = []
        self.fetched_code = 0
        self.scenario_hits = []


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
            elif self.path.startswith("/scenarios/") and self.path.endswith("/file"):
                # 场景文件降级供给（T12）：{id}--{version} 命中 200，否则 404
                state.scenario_hits.append(self.path)
                parts = self.path.strip("/").split("/")
                key = f"{parts[1]}--{parts[2]}" if len(parts) == 4 else ""
                if key in state.scenario_files:
                    self._json(200, state.scenario_files[key])
                else:
                    self._json(404, {"detail": "scenario file unreachable"})
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
                                     "execution_target": "real"})
                else:
                    self._json(204, None)
            elif self.path.endswith("/heartbeat"):
                state.task_heartbeats += 1
                self._json(200, {"task_id": 1, "executable_case_id": 1,
                                 "execution_target": "real"})
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

    def _start(code, scenario_files=None):
        state = _FakeBackend(code, scenario_files=scenario_files)
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
            "e2e-real-01",
            "--capabilities",
            "real",
            "--work-root",
            str(work_root),
            "--poll-interval",
            "0.1",
            "--heartbeat-interval",
            "0.1",
            "--task-timeout",
            "60",
            "--aw-package-dir",
            str(FIXTURE_AW),
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

    assert state.registered and state.registered[0]["worker_id"] == "e2e-real-01"
    assert state.registered[0]["capabilities"] == ["real"]
    assert state.claims == 1
    assert state.fetched_code == 1
    assert len(state.results) == 1
    result = state.results[0]
    assert result["worker_id"] == "e2e-real-01"
    assert result["verdict"] == "passed"
    assert "1 passed" in result["logs"]
    # Allure 结果经报告协议随回传落库（T12，Issue #13）
    assert result["allure_report"]["steps"][0]["status"] == "passed"
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


def test_e2e_long_running_artifacts_reported(backend, tmp_path):
    """长时操作制品随结果回传：name/kind/uri/checksum（故事 54，文件不出 testbed）。"""
    base_url, state = backend(LONG_RUNNING_CODE)
    proc = _run_worker(base_url, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert len(state.results) == 1
    result = state.results[0]
    assert result["verdict"] == "passed"
    assert len(result["artifacts"]) == 1
    artifact = result["artifacts"][0]
    assert artifact["kind"] == "log_package"
    assert artifact["uri"].startswith("file:///testbed/artifacts/")
    assert artifact["checksum"].startswith("sha256:")


def test_e2e_play_scenario_fetch_upload_fallback(backend, tmp_path):
    """无直通通道：Worker 凭 scenario_id+version 从后端拉取场景文件后上传（故事 53）。"""
    base_url, state = backend(
        PLAY_SCENARIO_CODE, scenario_files={"SC-E2E--v1": {"scenario": "demo"}}
    )
    proc = _run_worker(base_url, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert state.scenario_hits == ["/scenarios/SC-E2E/v1/file"]
    assert state.results[0]["verdict"] == "passed"


def test_e2e_play_scenario_unreachable_is_env_failed(backend, tmp_path):
    """场景文件不可达：归类环境类失败 env_failed，区别于断言失败（故事 53）。"""
    base_url, state = backend(PLAY_SCENARIO_CODE)  # 场景库为空：拉取 404
    proc = _run_worker(base_url, tmp_path)
    assert proc.returncode == 0, proc.stderr  # 任务失败不拖垮 Worker 进程
    assert len(state.results) == 1
    result = state.results[0]
    assert result["verdict"] == "env_failed"
    assert "scenario_unreachable" in result["logs"]
