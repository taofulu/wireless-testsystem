"""系统级测试：real 通路真实执行（T12，Issue #13，故事 30-33、53-54）。

守护的验收标准：
- execute 入队的 real 任务只被 real Worker 领取；用例状态随领取/回传迁移
  queued → running → done（通用能力路由与超时重领机制由 test_execution 守护，
  本文件守护 real 通路的用例状态机与结果载荷）
- 心跳超时/Worker 崩溃后 real 任务可重新领取且不重复产出结果（迟到的原
  Worker 回传 409，结果不被覆盖、用例只迁移一次）
- Allure 结果与制品（name/kind/uri/checksum）回传落库 execution_result，经
  GET /executable-cases/{id}/executions 对结果页可见；文件本身不入库
- env_failed（场景文件不可达等环境类失败）作为独立判决接收/落库/呈现，
  区别于断言失败（故事 53）
- 场景文件供给端点：命中 200、缺失 404、非法引用 422（无直通时 Worker 凭
  ID+版本拉取的降级通道）
"""
import json
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.db import SessionLocal
from app.models.execution import ExecutionTask

from tests.test_execution import (
    _claim,
    _make_executable_case,
    _register,
)

# fake_cli 夹具统一由 conftest 提供（系统边界 fake，ADR-0006）


def _execute_ready(client, exec_id: int) -> dict:
    """经 execute 闸门（二次确认 inconclusive）+ LASS ready 入队一条 real 任务。"""
    resp = client.post(
        f"/executable-cases/{exec_id}/execute", json={"confirm_inconclusive": True}
    )
    assert resp.status_code == 201, resp.text
    task = resp.json()
    assert task["execution_target"] == "real"
    assert task["status"] == "queued"
    assert task["env_check_result"] == "ready"
    return task


def _submit(client, task_id: int, worker_id: str, **fields):
    payload = {
        "worker_id": worker_id,
        "verdict": "passed",
        "logs": "ok",
        "step_results": [],
        "artifacts": [],
        **fields,
    }
    return client.post(f"/worker/tasks/{task_id}/result", json=payload)


# ---------------------------------------------------------------------------
# 全链路：execute → 领取 → running → 回传 → done（故事 30-33）
# ---------------------------------------------------------------------------


def test_real_full_path_state_machine_and_result_payload(
    client, fake_cli, fake_lass, monkeypatch
):
    made = _make_executable_case(client, fake_cli, monkeypatch)
    exec_id = made["executable"]["id"]
    case_id = made["case"]["id"]

    task = _execute_ready(client, exec_id)
    assert client.get(f"/text-cases/{case_id}").json()["status"] == "queued"

    # real 任务只被 real Worker 领取（双向隔离）
    _register(client, "sandbox-01", ("sandbox",))
    assert _claim(client, "sandbox-01", ("sandbox",)).status_code == 204

    _register(client, "real-01", ("real",))
    assert _claim(client, "real-01", ("real",)).json()["task_id"] == task["id"]
    assert client.get(f"/text-cases/{case_id}").json()["status"] == "running"

    allure_report = {"steps": [{"title": "步骤1: 激活目标小区", "status": "passed"}]}
    artifacts = [
        {
            "name": "export_logs-alarm",
            "kind": "log_package",
            "uri": "file:///testbed/artifacts/export_logs-alarm.zip",
            "checksum": "sha256:9f2c01ab",
        }
    ]
    resp = _submit(
        client,
        task["id"],
        "real-01",
        logs="1 passed in 0.4s",
        artifacts=artifacts,
        allure_report=allure_report,
    )
    assert resp.status_code == 201, resp.text
    assert client.get(f"/text-cases/{case_id}").json()["status"] == "done"

    records = client.get(f"/executable-cases/{exec_id}/executions").json()
    assert len(records) == 1
    rec = records[0]
    assert rec["id"] == task["id"]
    assert rec["status"] == "done"
    assert rec["worker_id"] == "real-01"
    assert rec["env_check_result"] == "ready"
    # 结果页数据源：Allure 结果原文 + 制品路径/校验和（文件本身不入库，故事 54）
    assert rec["result"]["verdict"] == "passed"
    assert rec["result"]["allure_report"] == allure_report
    assert rec["result"]["artifacts"] == artifacts


def test_env_failed_verdict_recorded_and_distinguishable(
    client, fake_cli, fake_lass, monkeypatch
):
    """场景文件不可达等环境类失败：env_failed 判决落库，与断言失败区分（故事 53）。"""
    made = _make_executable_case(client, fake_cli, monkeypatch)
    exec_id = made["executable"]["id"]
    task = _execute_ready(client, exec_id)
    _register(client, "real-01", ("real",))
    _claim(client, "real-01", ("real",))

    resp = _submit(
        client,
        task["id"],
        "real-01",
        verdict="env_failed",
        logs="scenario SC-X v9 unreachable: 404",
        step_results=[{"seq": 1, "status": "fail", "error_code": "scenario_unreachable"}],
    )
    assert resp.status_code == 201, resp.text

    rec = client.get(f"/executable-cases/{exec_id}/executions").json()[0]
    assert rec["result"]["verdict"] == "env_failed"
    assert rec["result"]["step_results"][0]["error_code"] == "scenario_unreachable"
    assert client.get(f"/text-cases/{made['case']['id']}").json()["status"] == "done"


# ---------------------------------------------------------------------------
# 故障恢复：崩溃重领且不重复产出结果（故事 32）
# ---------------------------------------------------------------------------


def test_real_task_reclaim_after_crash_no_duplicate_result(
    client, fake_cli, fake_lass, monkeypatch
):
    made = _make_executable_case(client, fake_cli, monkeypatch)
    exec_id = made["executable"]["id"]
    case_id = made["case"]["id"]
    task = _execute_ready(client, exec_id)
    _register(client, "real-01", ("real",))
    _register(client, "real-02", ("real",))
    assert _claim(client, "real-01", ("real",)).json()["task_id"] == task["id"]
    assert client.get(f"/text-cases/{case_id}").json()["status"] == "running"

    # 模拟 real-01 崩溃：任务心跳停在超时之前，real-02 重领
    db = SessionLocal()
    try:
        row = db.get(ExecutionTask, task["id"])
        row.heartbeat_at = datetime.now(timezone.utc) - timedelta(seconds=3600)
        db.commit()
    finally:
        db.close()

    assert _claim(client, "real-02", ("real",)).json()["task_id"] == task["id"]
    assert _submit(client, task["id"], "real-02", verdict="passed").status_code == 201
    assert client.get(f"/text-cases/{case_id}").json()["status"] == "done"

    # 原 Worker 迟到回传：409，已落库结果不被覆盖（不重复产出结果）
    resp = _submit(client, task["id"], "real-01", verdict="failed", logs="late")
    assert resp.status_code == 409
    rec = client.get(f"/executable-cases/{exec_id}/executions").json()[0]
    assert rec["result"]["verdict"] == "passed"
    assert client.get(f"/text-cases/{case_id}").json()["status"] == "done"


# ---------------------------------------------------------------------------
# 正式执行历史（spec：只查 real，沙盒调试会话不混入）
# ---------------------------------------------------------------------------


def test_executions_history_only_real_tasks(client, fake_cli, fake_lass, monkeypatch):
    made = _make_executable_case(client, fake_cli, monkeypatch)
    exec_id = made["executable"]["id"]

    resp = client.post(f"/executable-cases/{exec_id}/debug")
    assert resp.status_code == 201  # 沙盒调试任务不进正式执行历史

    task = _execute_ready(client, exec_id)
    records = client.get(f"/executable-cases/{exec_id}/executions").json()
    assert [r["id"] for r in records] == [task["id"]]
    assert records[0]["execution_target"] == "real"
    assert records[0]["result"] is None  # 未回传：结果页呈现"执行中"


# 阻断→复检产生多条 real 记录的"新在前"排序由 test_env_check 的
# recheck 用例（断言 executions 历史顺序）守护。


# ---------------------------------------------------------------------------
# 场景文件供给端点（故事 53：Worker 无直通时的拉取降级通道）
# ---------------------------------------------------------------------------


def test_scenario_file_endpoint_serves_and_guards(client, tmp_path, monkeypatch):
    (tmp_path / "SC-X--v2.json").write_text(
        json.dumps({"scenario": "demo"}), encoding="utf-8"
    )
    monkeypatch.setattr(settings, "scenario_files_dir", str(tmp_path), raising=False)

    resp = client.get("/scenarios/SC-X/v2/file")
    assert resp.status_code == 200
    assert resp.json() == {"scenario": "demo"}

    # 文件缺失即场景不可达（404）——Worker 侧归类为环境类失败
    assert client.get("/scenarios/SC-X/v9/file").status_code == 404
    # 非法引用（路径穿越成分）一律 422，不触文件系统
    assert client.get("/scenarios/bad%5Cid/v2/file").status_code == 422
