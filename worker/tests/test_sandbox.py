"""沙盒内核测试（T9）：纯函数、运行时与真实 pytest 子进程端到端。

三层守护：
1. 纯函数：deep_merge / build_initial_state / render_template / apply_descriptor /
   validate_stub_params / worst_level / aggregate_verdict / merge_step_results
   （与后端 app.sandbox 同一判定表，两侧独立实现各自单测守护）
2. SandboxRuntime：declarative 写状态可读回、stub 校验失败、组合操作序列、
   未知调用记 fail
3. run_sandbox_case 端到端：真实 pytest 子进程 + 沙盒 allure/aw 包，验证
   passed / failed / inconclusive 三态判决与 preset 生效
"""
import json
from pathlib import Path

import pytest

from wts_worker.runner import run_sandbox_case
from wts_worker.sandbox import (
    DEFAULT_TOPOLOGY,
    SandboxRuntime,
    aggregate_verdict,
    apply_descriptor,
    build_initial_state,
    deep_merge,
    merge_step_results,
    render_template,
    validate_stub_params,
    worst_level,
)


# ---------------------------------------------------------------------------
# 纯函数：状态与模板
# ---------------------------------------------------------------------------


def test_deep_merge_nested_override():
    base = {"a": {"x": 1, "y": 2}, "b": [1, 2]}
    assert deep_merge(base, {"a": {"y": 3}, "b": [9]}) == {"a": {"x": 1, "y": 3}, "b": [9]}


def test_build_initial_state_default_deep_copied():
    state = build_initial_state(None)
    assert state == DEFAULT_TOPOLOGY
    state["bbu"]["cells"]["1"] = {}
    assert DEFAULT_TOPOLOGY["bbu"]["cells"] == {}  # 常量不被污染


def test_build_initial_state_preset_overrides():
    state = build_initial_state({"instrument": {"rf_power_dbm": -10}, "bbu": {"cells": {"1": {"band": 78}}}})
    assert state["instrument"]["rf_power_dbm"] == -10
    assert state["instrument"]["connected"] is True  # 默认态保留
    assert state["bbu"]["cells"]["1"]["band"] == 78


def test_render_template_param_whole_preserves_type():
    assert render_template("<cell_id>", {"cell_id": 5}, {}) == 5


def test_render_template_param_embedded_in_path():
    assert render_template("bbu.cell_<cell_id>.admin_state", {"cell_id": 5}, {}) == "bbu.cell_5.admin_state"


def test_render_template_state_readback():
    state = {"instrument": {"rf_power_dbm": -20}}
    assert render_template("@instrument.rf_power_dbm", {}, state) == -20


def test_render_template_state_readback_with_param_in_path():
    """@路径中的 <param> 占位符先替换再查询（组合 @ 与 <> 的场景）。"""
    state = {"bbu": {"cell_5": {"admin_state": "active"}}}
    assert render_template("@bbu.cell_<cell_id>.admin_state", {"cell_id": 5}, state) == "active"


def test_render_template_missing_param_raises():
    with pytest.raises(KeyError):
        render_template("<nope>", {}, {})


def test_apply_descriptor_writes_then_returns():
    state = build_initial_state(None)
    result = apply_descriptor(
        state,
        {
            "writes": [{"path": "instrument.rf_power_dbm", "value": "<power_dbm>"}],
            "returns": {"ok": True, "rf_power_dbm": "@instrument.rf_power_dbm"},
        },
        {"power_dbm": -15},
    )
    assert state["instrument"]["rf_power_dbm"] == -15
    assert result == {"ok": True, "rf_power_dbm": -15}


def test_apply_descriptor_defaults_ok_true():
    result = apply_descriptor({}, {"returns": {}}, {})
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# 纯函数：桩校验
# ---------------------------------------------------------------------------


def test_validate_stub_params_passes():
    schema = {
        "required": ["cell_id"],
        "properties": {"cell_id": {"type": "integer"}},
    }
    assert validate_stub_params(schema, {"cell_id": 1}) is None


def test_validate_stub_params_missing_required():
    schema = {"required": ["cell_id"], "properties": {}}
    assert "缺少必填参数" in validate_stub_params(schema, {})


def test_validate_stub_params_wrong_type():
    schema = {"properties": {"cell_id": {"type": "integer"}}}
    assert "类型" in validate_stub_params(schema, {"cell_id": "abc"})


def test_validate_stub_params_bool_not_int():
    """bool 是 int 子类，但 JSON Schema 语义下 boolean ≠ integer。"""
    schema = {"properties": {"flag": {"type": "integer"}}}
    assert validate_stub_params(schema, {"flag": True}) is not None


# ---------------------------------------------------------------------------
# 纯函数：级别聚合与三态判决（与后端同一判定表）
# ---------------------------------------------------------------------------


def test_worst_level_semantics():
    assert worst_level([]) == "unsimulated"
    assert worst_level(["simulated", "schema_stub"]) == "schema_stub"
    assert worst_level(["simulated", "unsimulated"]) == "unsimulated"


def test_aggregate_verdict_truth_table():
    assert aggregate_verdict([]) == "failed"
    assert aggregate_verdict([{"status": "pass", "sim_level": "simulated"}]) == "passed"
    assert aggregate_verdict([{"status": "fail", "sim_level": "simulated"}]) == "failed"
    assert aggregate_verdict([{"status": "not_run", "sim_level": "simulated"}]) == "failed"
    assert aggregate_verdict([{"status": "pass", "sim_level": "schema_stub"}]) == "inconclusive"
    assert aggregate_verdict([{"status": "pass", "sim_level": "unsimulated"}]) == "inconclusive"
    # fail 优先于 inconclusive
    assert aggregate_verdict(
        [{"status": "fail", "sim_level": "simulated"}, {"status": "pass", "sim_level": "schema_stub"}]
    ) == "failed"


def test_merge_step_results_missing_is_not_run():
    merged = merge_step_results(
        [{"seq": 1, "op_name": "a", "simulatable": "simulated"},
         {"seq": 2, "op_name": "b", "simulatable": "schema_stub"}],
        {1: {"seq": 1, "op": "a", "sim_level": "simulated", "status": "pass"}},
    )
    assert merged[0]["status"] == "pass"
    assert merged[1]["status"] == "not_run"
    assert merged[1]["sim_level"] == "schema_stub"  # 覆盖事实不因未运行改变


# ---------------------------------------------------------------------------
# SandboxRuntime：分发与记录
# ---------------------------------------------------------------------------


def _runtime(steps, preset=None, tmp_path=None) -> SandboxRuntime:
    return SandboxRuntime(
        {"steps": steps, "preset": preset},
        tmp_path or Path("/tmp/wts-test-runtime"),
    )


def test_runtime_declarative_call_writes_state(tmp_path):
    rt = _runtime(
        [
            {
                "seq": 1,
                "op_name": "set_rf_power",
                "kind": "instrument_primitive",
                "simulatable": "simulated",
                "descriptor": {
                    "writes": [{"path": "instrument.rf_power_dbm", "value": "<power_dbm>"}],
                    "returns": {"ok": True, "rf_power_dbm": "@instrument.rf_power_dbm"},
                },
            }
        ],
        tmp_path=tmp_path,
    )
    rt.begin_step("步骤1: 设置射频功率")
    result = rt.call("instrument", "set_rf_power", {"power_dbm": -30})
    assert result["ok"] is True
    assert result["rf_power_dbm"] == -30  # 设置后可读回（有状态仿真）
    rt.end_step(None)
    assert rt.records[1]["status"] == "pass"
    assert rt.records[1]["sim_level"] == "simulated"


def test_runtime_unknown_op_marks_fail(tmp_path):
    rt = _runtime([], tmp_path=tmp_path)
    rt.begin_step("步骤1: 未知")
    result = rt.call("bbu", "nonexistent", {})
    assert result["ok"] is False
    rt.end_step(None)
    assert rt.records[1]["status"] == "fail"


def test_runtime_stub_validation_failure_marks_fail(tmp_path):
    rt = _runtime(
        [
            {
                "seq": 1,
                "op_name": "run_mml",
                "kind": "mml_generic",
                "simulatable": "schema_stub",
                "params_schema": {"required": ["command"], "properties": {"command": {"type": "string"}}},
            }
        ],
        tmp_path=tmp_path,
    )
    rt.begin_step("步骤1: 执行 MML")
    result = rt.call("bbu", "run_mml", {"args": {}})  # 缺 command
    assert result["ok"] is False
    rt.end_step(None)
    assert rt.records[1]["status"] == "fail"
    assert "桩校验失败" in rt.records[1]["detail"]


def test_runtime_stub_pass_records_schema_stub_level(tmp_path):
    rt = _runtime(
        [
            {
                "seq": 1,
                "op_name": "run_mml",
                "kind": "mml_generic",
                "simulatable": "schema_stub",
                "params_schema": {"required": ["command"]},
            }
        ],
        tmp_path=tmp_path,
    )
    rt.begin_step("步骤1: 执行 MML")
    rt.call("bbu", "run_mml", {"command": "LST CELL", "args": {}})
    rt.end_step(None)
    assert rt.records[1]["status"] == "pass"
    assert rt.records[1]["sim_level"] == "schema_stub"


def test_runtime_composite_runs_suboperations_in_order(tmp_path):
    rt = _runtime(
        [
            {
                "seq": 1,
                "op_name": "play_scenario",
                "kind": "composite",
                "simulatable": "simulated",
                "suboperations": [
                    {
                        "name": "play_file",
                        "simulatable": "simulated",
                        "descriptor": {
                            "writes": [{"path": "instrument.playing.state", "value": "starting"}],
                            "returns": {"ok": True},
                        },
                    },
                    {
                        "name": "wait_ready",
                        "simulatable": "simulated",
                        "descriptor": {
                            "writes": [{"path": "instrument.playing.state", "value": "ready"}],
                            "returns": {"ok": True, "state": "@instrument.playing.state"},
                        },
                    },
                ],
            }
        ],
        tmp_path=tmp_path,
    )
    rt.begin_step("步骤1: 播放场景")
    result = rt.call("instrument", "play_scenario", {"scenario_id": "s1", "scenario_version": "v1"})
    assert result["ok"] is True
    rt.end_step(None)
    assert rt.state["instrument"]["playing"]["state"] == "ready"


def test_runtime_preset_overrides_initial_state(tmp_path):
    rt = _runtime([], preset={"instrument": {"rf_power_dbm": -7}}, tmp_path=tmp_path)
    assert rt.state["instrument"]["rf_power_dbm"] == -7
    assert rt.state["instrument"]["connected"] is True


def test_runtime_step_exception_marks_fail(tmp_path):
    rt = _runtime(
        [{"seq": 1, "op_name": "act_cell", "kind": "mml_family", "simulatable": "simulated",
          "descriptor": {"returns": {"ok": True}}}],
        tmp_path=tmp_path,
    )
    rt.begin_step("步骤1: 激活小区")
    rt.call("bbu", "act_cell", {"cell_id": 1})
    rt.end_step(AssertionError("断言失败"))
    assert rt.records[1]["status"] == "fail"
    assert "AssertionError" in rt.records[1]["detail"]


def test_runtime_produces_artifacts_recorded(tmp_path):
    rt = _runtime(
        [{"seq": 1, "op_name": "export_logs", "kind": "long_running", "simulatable": "simulated",
          "produces_artifacts": True, "descriptor": {"returns": {"ok": True}}}],
        tmp_path=tmp_path,
    )
    rt.begin_step("步骤1: 导出日志")
    rt.call("bbu", "export_logs", {"log_type": "alarm"})
    rt.end_step(None)
    assert len(rt.artifacts) == 1
    assert rt.artifacts[0]["kind"] == "log_package"


def test_runtime_finalize_writes_report(tmp_path):
    rt = _runtime(
        [{"seq": 1, "op_name": "act_cell", "kind": "mml_family", "simulatable": "simulated",
          "descriptor": {"returns": {"ok": True}}}],
        tmp_path=tmp_path,
    )
    rt.begin_step("步骤1: 激活小区")
    rt.call("bbu", "act_cell", {"cell_id": 1})
    rt.end_step(None)
    rt.finalize()
    report = json.loads((tmp_path / "sandbox_report.json").read_text(encoding="utf-8"))
    assert report["steps"][0]["seq"] == 1


# ---------------------------------------------------------------------------
# run_sandbox_case：真实 pytest 子进程端到端
# ---------------------------------------------------------------------------

PASSING_CODE = '''"""沙盒演示用例"""

import allure

import aw


@allure.suite("demo")
def test_demo_pass():
    with allure.step("步骤1: 激活小区"):
        result_1 = aw.bbu.act_cell(cell_id=1)
        assert result_1.ok
'''

FAILING_CODE = '''"""沙盒失败用例"""

import allure

import aw


@allure.suite("demo")
def test_demo_fail():
    with allure.step("步骤1: 激活小区"):
        result_1 = aw.bbu.act_cell(cell_id=1)
        assert not result_1.ok, "期望失败"
'''

TWO_STEP_CODE = '''"""两步混合仿真级别用例"""

import allure

import aw


@allure.suite("demo")
def test_two_steps():
    with allure.step("步骤1: 激活小区"):
        r1 = aw.bbu.act_cell(cell_id=1)
        assert r1.ok
    with allure.step("步骤2: 执行 MML"):
        r2 = aw.bbu.run_mml(command="LST CELL", args={})
        assert r2.ok
'''


def _context(steps, preset=None):
    return {"executable_case_id": 1, "preset": preset, "steps": steps}


ACT_CELL_STEP = {
    "seq": 1,
    "op_name": "act_cell",
    "kind": "mml_family",
    "device_target": "bbu",
    "simulatable": "simulated",
    "params_schema": {"required": ["cell_id"]},
    "descriptor": {
        "writes": [{"path": "bbu.cell_<cell_id>.admin_state", "value": "active"}],
        "returns": {"ok": True, "admin_state": "@bbu.cell_<cell_id>.admin_state"},
    },
    "suboperations": [],
}

RUN_MML_STEP = {
    "seq": 2,
    "op_name": "run_mml",
    "kind": "mml_generic",
    "device_target": "bbu",
    "simulatable": "schema_stub",
    "params_schema": {"required": ["command", "args"]},
    "descriptor": None,
    "suboperations": [],
}


def test_sandbox_case_passing_is_passed(tmp_path):
    outcome = run_sandbox_case(PASSING_CODE, _context([ACT_CELL_STEP]), tmp_path)
    assert outcome.verdict == "passed"
    assert outcome.step_results[0]["status"] == "pass"
    assert outcome.step_results[0]["sim_level"] == "simulated"
    assert "1 passed" in outcome.logs
    assert list(tmp_path.iterdir()) == []  # 临时目录清理


def test_sandbox_case_assertion_failure_is_failed(tmp_path):
    outcome = run_sandbox_case(FAILING_CODE, _context([ACT_CELL_STEP]), tmp_path)
    assert outcome.verdict == "failed"
    assert outcome.step_results[0]["status"] == "fail"


def test_sandbox_case_stub_step_is_inconclusive(tmp_path):
    """含仅桩校验步骤：执行通过但判决 inconclusive（禁止假绿，ADR-0009）。"""
    outcome = run_sandbox_case(TWO_STEP_CODE, _context([ACT_CELL_STEP, RUN_MML_STEP]), tmp_path)
    assert outcome.verdict == "inconclusive"
    levels = {r["seq"]: r["sim_level"] for r in outcome.step_results}
    assert levels == {1: "simulated", 2: "schema_stub"}


def test_sandbox_case_missing_report_marks_not_run(tmp_path):
    """子进程早夭（语法错误）→ 报告缺失 → 全部 not_run → failed（诚实优先）。"""
    outcome = run_sandbox_case("def broken(:\n", _context([ACT_CELL_STEP]), tmp_path)
    assert outcome.verdict == "failed"
    assert outcome.step_results[0]["status"] == "not_run"
