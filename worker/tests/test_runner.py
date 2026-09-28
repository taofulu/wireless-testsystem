"""real 通路执行器单测（T12，Issue #13）：真实 pytest 子进程 + 伪 testbed AW 夹具。

守护的验收标准：
- 执行器以真实 pytest 子进程守护（伪 testbed AW 夹具注入 PYTHONPATH）
- Allure 结果经 real_report.json 报告协议回收（allure_report）
- long_running 操作产出制品（name/kind/uri/checksum），文件本身不出 testbed
- play_scenario 场景文件传递：直通优先（FAKE_AW_DIRECT_LINK），否则凭
  scenario_id+version 从后端拉取后上传；不可达归类 env_failed（环境类失败
  区别于断言失败，故事 53）
- 临时目录执行后清理（故事 30）；超时兜底 failed；未配置 AW 包诚实失败
"""
import http.server
import json
import socketserver
import threading
from pathlib import Path

import pytest

from wts_worker.runner import run_real_case

FIXTURE_AW = Path(__file__).parent / "fixtures" / "testbed_aw"

PASSING_CODE = '''"""演示用例"""

import allure

import aw


@allure.parent_suite("Wireless Test System")
@allure.suite("demo")
def test_demo_pass():
    with allure.step("步骤1: 激活小区"):
        result_1 = aw.bbu.act_cell(cell_id=1)
        assert result_1.ok
'''

FAILING_CODE = '''"""失败用例"""

import allure

import aw


@allure.suite("demo")
def test_demo_fail():
    with allure.step("步骤1: 激活小区"):
        result_1 = aw.bbu.act_cell(cell_id=1)
        assert not result_1.ok, "期望失败"
'''

LONG_RUNNING_CODE = '''"""长时操作用例"""

import allure

import aw


@allure.suite("demo")
def test_export_logs():
    with allure.step("步骤1: 导出日志"):
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
    with allure.step("步骤1: 上传并播放场景文件"):
        result_1 = aw.instrument.play_scenario(
            scenario_id='SC-E2E', scenario_version='v1')
        assert result_1.ok
'''


class _ScenarioServer:
    """最小场景文件供给：/scenarios/{id}/{version}/file → 200 / 403 / 404。"""

    def __init__(self, files: dict, denied: set = None):
        self.files = files
        self.denied = denied or set()
        self.hits: list[str] = []

        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.hits.append(self.path)
                parts = self.path.strip("/").split("/")
                if len(parts) == 4 and parts[0] == "scenarios" and parts[3] == "file":
                    key = f"{parts[1]}--{parts[2]}"
                    if key in outer.denied:
                        self.send_response(403)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    if key in outer.files:
                        body = json.dumps(outer.files[key]).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def shutdown(self) -> None:
        self._server.shutdown()


@pytest.fixture()
def scenario_server():
    holder = []

    def _start(files: dict, denied: set = None) -> _ScenarioServer:
        server = _ScenarioServer(files, denied)
        holder.append(server)
        return server

    yield _start
    for server in holder:
        server.shutdown()


def test_passing_case_collects_allure_report_and_cleans_up(tmp_path):
    outcome = run_real_case(PASSING_CODE, tmp_path, aw_package_dir=str(FIXTURE_AW))
    assert outcome.verdict == "passed"
    assert "1 passed" in outcome.logs
    # Allure 结果经报告协议回收（步骤标题与状态）
    assert outcome.allure_report is not None
    titles = [s["title"] for s in outcome.allure_report["steps"]]
    assert titles == ["步骤1: 激活小区"]
    assert outcome.allure_report["steps"][0]["status"] == "passed"
    # 临时落盘执行后清理（故事 30）
    assert list(tmp_path.iterdir()) == []


def test_failing_assertion_is_failed_not_env_failed(tmp_path):
    outcome = run_real_case(FAILING_CODE, tmp_path, aw_package_dir=str(FIXTURE_AW))
    assert outcome.verdict == "failed"  # 断言失败不得归类为环境类失败
    assert "1 failed" in outcome.logs
    assert list(tmp_path.iterdir()) == []


def test_long_running_artifacts_collected(tmp_path):
    """长时操作制品：name/kind/uri/checksum 回收，文件本身不出 testbed（故事 54）。"""
    outcome = run_real_case(LONG_RUNNING_CODE, tmp_path, aw_package_dir=str(FIXTURE_AW))
    assert outcome.verdict == "passed"
    assert len(outcome.artifacts) == 1
    artifact = outcome.artifacts[0]
    assert artifact["name"] == "export_logs-alarm"
    assert artifact["kind"] == "log_package"
    assert artifact["uri"].startswith("file:///testbed/artifacts/")
    assert artifact["checksum"].startswith("sha256:")


def test_play_scenario_fetch_upload_when_no_direct_link(tmp_path, scenario_server, monkeypatch):
    """无直通：凭 scenario_id+version 从后端拉取后上传（降级通道，故事 53）。"""
    monkeypatch.delenv("FAKE_AW_DIRECT_LINK", raising=False)
    server = scenario_server({"SC-E2E--v1": {"scenario": "demo"}})
    outcome = run_real_case(
        PLAY_SCENARIO_CODE,
        tmp_path,
        aw_package_dir=str(FIXTURE_AW),
        server_url=server.url,
    )
    assert outcome.verdict == "passed"
    assert server.hits == ["/scenarios/SC-E2E/v1/file"]


def test_play_scenario_direct_link_skips_fetch(tmp_path, scenario_server, monkeypatch):
    """MBB↔仪表直通优先：只传引用，不经过 Worker 拉取（故事 53）。"""
    monkeypatch.setenv("FAKE_AW_DIRECT_LINK", "1")
    server = scenario_server({})  # 无可供文件：被拉取即 404
    outcome = run_real_case(
        PLAY_SCENARIO_CODE,
        tmp_path,
        aw_package_dir=str(FIXTURE_AW),
        server_url=server.url,
    )
    assert outcome.verdict == "passed"
    assert server.hits == []


def test_play_scenario_unreachable_is_env_failed(tmp_path, scenario_server, monkeypatch):
    """场景文件不可达（404）：归类环境类失败 env_failed，区别于断言失败（故事 53）。"""
    monkeypatch.delenv("FAKE_AW_DIRECT_LINK", raising=False)
    server = scenario_server({})
    outcome = run_real_case(
        PLAY_SCENARIO_CODE,
        tmp_path,
        aw_package_dir=str(FIXTURE_AW),
        server_url=server.url,
    )
    assert outcome.verdict == "env_failed"
    assert "scenario_unreachable" in outcome.logs


def test_play_scenario_forbidden_is_env_failed(tmp_path, scenario_server, monkeypatch):
    """场景文件无权限（403）：同样归类环境类失败，区别于断言失败（故事 53）。"""
    monkeypatch.delenv("FAKE_AW_DIRECT_LINK", raising=False)
    server = scenario_server({}, denied={"SC-E2E--v1"})
    outcome = run_real_case(
        PLAY_SCENARIO_CODE,
        tmp_path,
        aw_package_dir=str(FIXTURE_AW),
        server_url=server.url,
    )
    assert outcome.verdict == "env_failed"
    assert "scenario_unreachable" in outcome.logs


def test_missing_aw_package_dir_fails_honestly(tmp_path):
    """未配置 AW 包目录：诚实失败，不静默退回桩包（不假绿）。"""
    outcome = run_real_case(PASSING_CODE, tmp_path, aw_package_dir="")
    assert outcome.verdict == "failed"
    assert "--aw-package-dir" in outcome.logs


def test_timeout_is_failed_and_cleans_up(tmp_path):
    code = "import time\n\ndef test_slow():\n    time.sleep(10)\n"
    outcome = run_real_case(
        code, tmp_path, timeout_seconds=0.5, aw_package_dir=str(FIXTURE_AW)
    )
    assert outcome.verdict == "failed"
    assert "超时" in outcome.logs
    assert list(tmp_path.iterdir()) == []
