"""sandbox 模块纯函数与系统级端到端测试（T9/T10）。

守护要点：
- 纯函数（worst_level / aggregate_verdict / uncovered_steps / merge_step_results）
  与 Worker 侧 wts_worker.sandbox 同一判定表，两侧独立实现、各自单测守护
- 后端装配（build_step_sims / build_sandbox_context / sandbox_meta）按操作目录
  + 结构化步骤正确聚合仿真级别（composite 最差子操作、场景元信息门）
- debug_run 保留策略、真实执行闸门（submit_real_execution）三态判定
"""
import json
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models.catalog import Operation
from app.models.execution import ExecutionTask
from app.models.mapping import MappingStatus, StructuredStep
from app.models.text_case import TextCase, TextCaseStatus
from app.sandbox import (
    aggregate_verdict,
    build_sandbox_context,
    build_step_sims,
    latest_debug_run,
    list_debug_runs,
    record_debug_run,
    sandbox_meta,
    uncovered_steps,
    worst_level,
)

FIXTURE = Path(__file__).parent / "fixtures" / "fake_glm_cli.py"


@pytest.fixture()
def fake_cli(monkeypatch):
    """与 test_execution 相同：GLM CLI 走 fake 脚本（系统边界 fake）。"""
    monkeypatch.setattr(settings, "glm_cli_path", str(FIXTURE), raising=False)
    monkeypatch.setattr(settings, "glm_elaboration_skill", "elaboration-skill", raising=False)
    monkeypatch.setattr(settings, "glm_mapping_skill", "mapping-skill", raising=False)
    monkeypatch.setattr(settings, "glm_timeout_seconds", 10.0, raising=False)
    for var in ("FAKE_GLM_MAPPING", "FAKE_GLM_ELABORATION", "FAKE_GLM_SLEEP_SECONDS"):
        monkeypatch.delenv(var, raising=False)
    from app import elaboration, mapping

    elaboration._running_jobs.clear()
    elaboration._active_procs.clear()
    mapping._running_jobs.clear()
    mapping._active_procs.clear()
    yield settings
    for var in ("FAKE_GLM_MAPPING", "FAKE_GLM_ELABORATION", "FAKE_GLM_SLEEP_SECONDS"):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# 纯函数（与 Worker 侧同一判定表）
# ---------------------------------------------------------------------------


def test_worst_level_empty_is_unsimulated():
    assert worst_level([]) == "unsimulated"


def test_worst_level_takes_worst():
    assert worst_level(["simulated", "schema_stub"]) == "schema_stub"
    assert worst_level(["simulated", "unsimulated"]) == "unsimulated"
    assert worst_level(["schema_stub", "unsimulated"]) == "unsimulated"
    assert worst_level(["simulated"]) == "simulated"


def test_aggregate_verdict_empty_is_failed():
    assert aggregate_verdict([]) == "failed"


def test_aggregate_verdict_any_fail_is_failed():
    assert aggregate_verdict([{"status": "pass"}, {"status": "fail"}]) == "failed"


def test_aggregate_verdict_not_run_is_failed():
    assert aggregate_verdict([{"status": "pass", "sim_level": "simulated"},
                               {"status": "not_run", "sim_level": "simulated"}]) == "failed"


def test_aggregate_verdict_uncovered_is_inconclusive():
    assert aggregate_verdict([{"status": "pass", "sim_level": "simulated"},
                               {"status": "pass", "sim_level": "schema_stub"}]) == "inconclusive"
    assert aggregate_verdict([{"status": "pass", "sim_level": "simulated"},
                               {"status": "pass", "sim_level": "unsimulated"}]) == "inconclusive"


def test_aggregate_verdict_all_simulated_pass_is_passed():
    assert aggregate_verdict([{"status": "pass", "sim_level": "simulated"},
                               {"status": "pass", "sim_level": "simulated"}]) == "passed"


def test_uncovered_steps_returns_unsimulated_seqs():
    assert uncovered_steps([{"seq": 1, "sim_level": "simulated"},
                             {"seq": 2, "sim_level": "schema_stub"},
                             {"seq": 3, "sim_level": "unsimulated"}]) == [2, 3]


# ---------------------------------------------------------------------------
# 系统级：装配与上下文
# ---------------------------------------------------------------------------


def _import_operations(client):
    data = json.loads((Path(settings.catalog_dir) / "operations.json").read_text(encoding="utf-8"))
    resp = client.post("/operations/import", json=data)
    assert resp.status_code == 201, resp.text
    return data


def _seed_text_case(db, status=TextCaseStatus.GENERATED):
    case = TextCase(
        title="demo",
        precondition="p",
        steps_text="s",
        expected_text="e",
        status=status,
    )
    db.add(case)
    db.commit()
    db.refresh(case)
    return case


def _seed_structured_steps(db, case_id, ops):
    for i, op in enumerate(ops, start=1):
        step = StructuredStep(
            text_case_id=case_id,
            seq=i,
            action_text=f"step {i}",
            aw_operation_id=op.id if op else None,
            params={"cell_id": 1} if op and op.name == "act_cell" else {},
            assertion_text="assert ok",
            mapping_status=MappingStatus.MAPPED if op else MappingStatus.UNMAPPED,
        )
        db.add(step)
    db.commit()


def _seed_executable_case(db, case_id):
    from app.models.executable_case import ExecutableCase

    ec = ExecutableCase(text_case_id=case_id, version=1, code="")
    db.add(ec)
    db.commit()
    db.refresh(ec)
    return ec


def test_build_step_sims_honors_composite_worst_level(client):
    _import_operations(client)
    db = SessionLocal()
    try:
        case = _seed_text_case(db)
        play = db.execute(select(Operation).where(Operation.name == "play_scenario")).scalar_one()
        _seed_structured_steps(db, case.id, [play])
        ec = _seed_executable_case(db, case.id)
        sims = build_step_sims(db, ec)
        assert len(sims) == 1
        # 演示目录子操作均为 declarative → simulated；场景元信息门无 has_meta 场景
        # 所以 effective 应为 unsimulated（ADR-0010 场景元信息门）
        assert sims[0].simulatable == "unsimulated"
        assert sims[0].kind == "composite"
        # 子操作列表应包含 4 个子操作
        assert len(sims[0].suboperations) == 4
    finally:
        db.close()


def test_build_step_sims_declarative_with_descriptor(client):
    _import_operations(client)
    db = SessionLocal()
    try:
        case = _seed_text_case(db)
        act = db.execute(select(Operation).where(Operation.name == "act_cell")).scalar_one()
        _seed_structured_steps(db, case.id, [act])
        ec = _seed_executable_case(db, case.id)
        sims = build_step_sims(db, ec)
        assert sims[0].simulatable == "simulated"
        assert sims[0].descriptor is not None
        assert sims[0].descriptor.get("writes") is not None
    finally:
        db.close()


def test_build_step_sims_schema_stub_no_descriptor(client):
    _import_operations(client)
    db = SessionLocal()
    try:
        case = _seed_text_case(db)
        run_mml = db.execute(select(Operation).where(Operation.name == "run_mml")).scalar_one()
        _seed_structured_steps(db, case.id, [run_mml])
        ec = _seed_executable_case(db, case.id)
        sims = build_step_sims(db, ec)
        assert sims[0].simulatable == "schema_stub"
        assert sims[0].descriptor is None
    finally:
        db.close()


def test_build_step_sims_unknown_op_is_unsimulated(client):
    db = SessionLocal()
    try:
        case = _seed_text_case(db)
        _seed_structured_steps(db, case.id, [None])
        ec = _seed_executable_case(db, case.id)
        sims = build_step_sims(db, ec)
        assert sims[0].simulatable == "unsimulated"
        assert sims[0].op_name == "(未知操作)"
    finally:
        db.close()


def test_sandbox_meta_has_uncovered_flag(client):
    _import_operations(client)
    db = SessionLocal()
    try:
        case = _seed_text_case(db)
        act = db.execute(select(Operation).where(Operation.name == "act_cell")).scalar_one()
        run_mml = db.execute(select(Operation).where(Operation.name == "run_mml")).scalar_one()
        _seed_structured_steps(db, case.id, [act, run_mml])
        ec = _seed_executable_case(db, case.id)
        meta = sandbox_meta(db, ec)
        assert meta["executable_case_id"] == ec.id
        assert len(meta["steps"]) == 2
        assert meta["has_uncovered"] is True  # run_mml 是 schema_stub
    finally:
        db.close()


def test_sandbox_context_includes_preset_and_params_schema(client):
    _import_operations(client)
    db = SessionLocal()
    try:
        case = _seed_text_case(db)
        act = db.execute(select(Operation).where(Operation.name == "act_cell")).scalar_one()
        _seed_structured_steps(db, case.id, [act])
        ec = _seed_executable_case(db, case.id)
        ctx = build_sandbox_context(db, ec, preset={"bbu": {"cells": {"1": {"band": 1}}}})
        assert ctx["preset"]["bbu"]["cells"]["1"]["band"] == 1
        step = ctx["steps"][0]
        assert step["params_schema"]["properties"]["cell_id"]["type"] == "integer"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 调试会话记录与保留策略
# ---------------------------------------------------------------------------


def _seed_task(db, ec_id, execution_target="sandbox", preset=None):
    task = ExecutionTask(
        executable_case_id=ec_id,
        execution_target=execution_target,
        preset=preset,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def test_record_debug_run_and_list_latest(client, monkeypatch):
    monkeypatch.setattr(settings, "debug_run_keep_latest", 3)
    db = SessionLocal()
    try:
        case = _seed_text_case(db)
        ec = _seed_executable_case(db, case.id)
        for i in range(5):
            task = _seed_task(db, ec.id, preset={"run": i})
            record_debug_run(
                db, task, verdict="passed", step_results=[{"seq": 1}], sim_package_version="1.0"
            )
        runs = list_debug_runs(db, ec.id)
        assert len(runs) == 3  # 保留最近 3 条
        # 新的在前
        assert runs[0].preset == {"run": 4}
        latest = latest_debug_run(db, ec.id)
        assert latest.preset == {"run": 4}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 真实执行闸门（T10）
# ---------------------------------------------------------------------------


def _full_flow_to_generated(client, fake_cli, monkeypatch):
    """从文本用例到 generated 态的完整链路（复用 test_execution 的 helper）。"""
    from tests.test_execution import (
        _import_demo_dictionary,
        _make_executable_case,
        _poll_mapping,
    )

    _import_demo_dictionary(client)
    monkeypatch.setenv("FAKE_GLM_MAPPING", "matched")
    case = client.post(
        "/text-cases",
        json={
            "title": "闸门测试",
            "precondition": "基站已加电",
            "steps_text": "1. 激活目标小区\n2. 查询小区状态",
            "expected_text": "小区激活成功",
        },
    ).json()
    client.post(f"/text-cases/{case['id']}/elaboration/skip")
    client.post(f"/text-cases/{case['id']}/map")
    _poll_mapping(client, case["id"])
    client.post(f"/text-cases/{case['id']}/confirm")
    resp = client.post(f"/text-cases/{case['id']}/generate")
    return case, resp.json()


def test_execute_passed_without_confirmation(client, fake_cli, monkeypatch):
    """最近一次沙盒 passed：直接放行，无需 confirm_inconclusive。"""
    from tests.test_execution import _register, _claim, _debug_task

    case, exe = _full_flow_to_generated(client, fake_cli, monkeypatch)
    task = _debug_task(client, exe["id"])
    _register(client, "sandbox-01", ("sandbox",))
    _claim(client, "sandbox-01")
    client.post(
        f"/worker/tasks/{task['id']}/result",
        json={"worker_id": "sandbox-01", "verdict": "passed", "logs": "ok",
              "step_results": [{"seq": 1, "sim_level": "simulated", "status": "pass"}]},
    )
    resp = client.post(f"/executable-cases/{exe['id']}/execute", json={"confirm_inconclusive": False})
    assert resp.status_code == 201, resp.text
    assert resp.json()["execution_target"] == "real"
    case_refreshed = client.get(f"/text-cases/{case['id']}").json()
    assert case_refreshed["status"] == "queued"


def test_execute_inconclusive_requires_confirmation(client, fake_cli, monkeypatch):
    """inconclusive 未带确认：409 并附 uncovered_steps。"""
    from tests.test_execution import _register, _claim, _debug_task

    case, exe = _full_flow_to_generated(client, fake_cli, monkeypatch)
    task = _debug_task(client, exe["id"])
    _register(client, "sandbox-01", ("sandbox",))
    _claim(client, "sandbox-01")
    client.post(
        f"/worker/tasks/{task['id']}/result",
        json={"worker_id": "sandbox-01", "verdict": "inconclusive", "logs": "ok",
              "step_results": [{"seq": 1, "sim_level": "schema_stub", "status": "pass"}]},
    )
    resp = client.post(f"/executable-cases/{exe['id']}/execute", json={"confirm_inconclusive": False})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "confirmation_required"
    assert detail["uncovered_steps"] == [1]


def test_execute_inconclusive_with_confirmation_passes(client, fake_cli, monkeypatch):
    """inconclusive 带 confirm_inconclusive=true：放行。"""
    from tests.test_execution import _register, _claim, _debug_task

    case, exe = _full_flow_to_generated(client, fake_cli, monkeypatch)
    task = _debug_task(client, exe["id"])
    _register(client, "sandbox-01", ("sandbox",))
    _claim(client, "sandbox-01")
    client.post(
        f"/worker/tasks/{task['id']}/result",
        json={"worker_id": "sandbox-01", "verdict": "inconclusive", "logs": "ok",
              "step_results": [{"seq": 1, "sim_level": "schema_stub", "status": "pass"}]},
    )
    resp = client.post(f"/executable-cases/{exe['id']}/execute", json={"confirm_inconclusive": True})
    assert resp.status_code == 201
    assert resp.json()["execution_target"] == "real"


def test_execute_failed_always_blocked(client, fake_cli, monkeypatch):
    """failed 一律拒绝，无论是否带 confirm。"""
    from tests.test_execution import _register, _claim, _debug_task

    case, exe = _full_flow_to_generated(client, fake_cli, monkeypatch)
    task = _debug_task(client, exe["id"])
    _register(client, "sandbox-01", ("sandbox",))
    _claim(client, "sandbox-01")
    client.post(
        f"/worker/tasks/{task['id']}/result",
        json={"worker_id": "sandbox-01", "verdict": "failed", "logs": "err",
              "step_results": [{"seq": 1, "sim_level": "simulated", "status": "fail"}]},
    )
    resp = client.post(f"/executable-cases/{exe['id']}/execute", json={"confirm_inconclusive": True})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "sandbox_failed"


def test_execute_no_debug_run_requires_confirmation(client, fake_cli, monkeypatch):
    """尚无调试结论：未覆盖步骤视为全部步骤，需二次确认。"""
    case, exe = _full_flow_to_generated(client, fake_cli, monkeypatch)
    resp = client.post(f"/executable-cases/{exe['id']}/execute", json={"confirm_inconclusive": False})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "confirmation_required"
    # 步骤数 = 2（激活小区 + 查询状态）
    assert resp.json()["detail"]["uncovered_steps"] == [1, 2]


def test_execute_requires_generated_status(client, fake_cli, monkeypatch):
    """非 generated 态用例提交真实执行被 409。"""
    case, exe = _full_flow_to_generated(client, fake_cli, monkeypatch)
    # 先让用例进入 queued（通过 execute）
    from tests.test_execution import _register, _claim, _debug_task

    task = _debug_task(client, exe["id"])
    _register(client, "sandbox-01", ("sandbox",))
    _claim(client, "sandbox-01")
    client.post(
        f"/worker/tasks/{task['id']}/result",
        json={"worker_id": "sandbox-01", "verdict": "passed", "logs": "ok", "step_results": []},
    )
    client.post(f"/executable-cases/{exe['id']}/execute", json={"confirm_inconclusive": False})
    # 再次 execute：status 已是 queued
    resp = client.post(f"/executable-cases/{exe['id']}/execute", json={"confirm_inconclusive": False})
    assert resp.status_code == 409
    assert "not_generated" in resp.json()["detail"]["code"]


# ---------------------------------------------------------------------------
# 路由级：debug-runs / sandbox-meta / sandbox-context
# ---------------------------------------------------------------------------


def test_debug_runs_returns_latest_n(client, fake_cli, monkeypatch):
    from tests.test_execution import _register, _claim, _debug_task

    case, exe = _full_flow_to_generated(client, fake_cli, monkeypatch)
    for _ in range(2):
        task = _debug_task(client, exe["id"])
        _register(client, "sandbox-01", ("sandbox",))
        _claim(client, "sandbox-01")
        client.post(
            f"/worker/tasks/{task['id']}/result",
            json={"worker_id": "sandbox-01", "verdict": "passed", "logs": "ok",
                  "step_results": [{"seq": 1, "sim_level": "simulated", "status": "pass"}]},
        )
    resp = client.get(f"/executable-cases/{exe['id']}/debug-runs")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["runs"]) == 2
    assert body["runs"][0]["verdict"] == "passed"
    assert body["runs"][0]["environment_disclaimer"] == "仿真执行、未进行环境校验"


def test_sandbox_meta_endpoint(client, fake_cli, monkeypatch):
    case, exe = _full_flow_to_generated(client, fake_cli, monkeypatch)
    resp = client.get(f"/executable-cases/{exe['id']}/sandbox-meta")
    assert resp.status_code == 200
    body = resp.json()
    assert body["executable_case_id"] == exe["id"]
    assert isinstance(body["steps"], list)
    assert "has_uncovered" in body


def test_sandbox_context_with_task_id(client, fake_cli, monkeypatch):
    from tests.test_execution import _register, _claim, _debug_task

    case, exe = _full_flow_to_generated(client, fake_cli, monkeypatch)
    task = _debug_task(client, exe["id"])
    # 更新任务 preset（直接 DB 操作，模拟后端创建时携带）
    db = SessionLocal()
    try:
        t = db.get(ExecutionTask, task["id"])
        t.preset = {"bbu": {"cells": {"1": {"band": 78}}}}
        db.commit()
    finally:
        db.close()
    resp = client.get(
        f"/executable-cases/{exe['id']}/sandbox-context", params={"task_id": task["id"]}
    )
    assert resp.status_code == 200
    assert resp.json()["preset"]["bbu"]["cells"]["1"]["band"] == 78


def test_sandbox_context_wrong_task_404(client, fake_cli, monkeypatch):
    case, exe = _full_flow_to_generated(client, fake_cli, monkeypatch)
    resp = client.get(
        f"/executable-cases/{exe['id']}/sandbox-context", params={"task_id": 9999}
    )
    assert resp.status_code == 404


def test_debug_preset_not_persisted_to_text_case(client, fake_cli, monkeypatch):
    """调试预设仅存于 debug_run 上下文，不写入用例正式数据（ADR-0009）。"""
    from tests.test_execution import _register, _claim, _debug_task

    case, exe = _full_flow_to_generated(client, fake_cli, monkeypatch)
    task = _debug_task(client, exe["id"])
    _register(client, "sandbox-01", ("sandbox",))
    _claim(client, "sandbox-01")
    client.post(
        f"/worker/tasks/{task['id']}/result",
        json={"worker_id": "sandbox-01", "verdict": "passed", "logs": "ok", "step_results": []},
    )
    case_refreshed = client.get(f"/text-cases/{case['id']}").json()
    assert case_refreshed.get("preset") is None  # 用例模型无 preset 字段


# ---------------------------------------------------------------------------
# T10 修复闭环（故事 43）：failed → reopen → 改文本重映射 → 重新生成 → 再调试通过
# ---------------------------------------------------------------------------


def test_failed_then_reopen_and_regenerate_loop(client, fake_cli, monkeypatch):
    """端到端修复闭环：沙盒 failed → 真实执行被拒 → reopen 回确认态 →
    改文本重新映射 → 生成新版本 → 新版本沙盒 passed → 真实执行放行。

    同时断言全流程不存在代码编辑入口（API 只对文本/映射开放写操作，
    executable_case 代码只读——写方法 405 由 T7 守护，此处验证闭环可用）。
    """
    from tests.test_execution import _register, _claim, _debug_task

    case, exe = _full_flow_to_generated(client, fake_cli, monkeypatch)

    # 1. 沙盒调试 failed
    task = _debug_task(client, exe["id"])
    _register(client, "sandbox-01", ("sandbox",))
    _claim(client, "sandbox-01")
    client.post(
        f"/worker/tasks/{task['id']}/result",
        json={"worker_id": "sandbox-01", "verdict": "failed", "logs": "assert err",
              "step_results": [{"seq": 1, "sim_level": "simulated", "status": "fail"}]},
    )
    # 2. failed 后真实执行一律被拒（即使带确认标记）
    resp = client.post(f"/executable-cases/{exe['id']}/execute",
                       json={"confirm_inconclusive": True})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "sandbox_failed"

    # 3. 只许上游修复：reopen 回确认态（generated → mapped），步骤原样保留
    resp = client.post(f"/text-cases/{case['id']}/reopen")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "mapped"
    assert resp.json()["step_count"] == 2  # 结构化步骤原样保留

    # 4. 重新确认 → 重新生成新版本（代码永远 100% 模板渲染，无编辑入口）
    assert client.post(f"/text-cases/{case['id']}/confirm").status_code == 200
    resp = client.post(f"/text-cases/{case['id']}/generate")
    assert resp.status_code == 201
    new_exe = resp.json()
    assert new_exe["version"] == exe["version"] + 1  # 新版本号
    assert new_exe["id"] != exe["id"]

    # 5. 新版本再次沙盒调试：passed
    task2 = client.post(f"/executable-cases/{new_exe['id']}/debug").json()
    _claim(client, "sandbox-01")
    client.post(
        f"/worker/tasks/{task2['id']}/result",
        json={"worker_id": "sandbox-01", "verdict": "passed", "logs": "ok",
              "step_results": [{"seq": 1, "sim_level": "simulated", "status": "pass"},
                                {"seq": 2, "sim_level": "simulated", "status": "pass"}]},
    )
    # 6. 新版本真实执行直接放行（旧版本的 failed 不影响新版本）
    resp = client.post(f"/executable-cases/{new_exe['id']}/execute",
                       json={"confirm_inconclusive": False})
    assert resp.status_code == 201


def test_reopen_rejected_when_not_generated(client, fake_cli, monkeypatch):
    """reopen 只在 generated 态可用；confirmed 态重开走另一语义，此处 409。"""
    _import_operations(client)
    db = SessionLocal()
    try:
        case = _seed_text_case(db, status=TextCaseStatus.CONFIRMED)
        case_id = case.id
    finally:
        db.close()
    resp = client.post(f"/text-cases/{case_id}/reopen")
    assert resp.status_code == 409
