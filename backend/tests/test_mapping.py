"""系统级测试：文本用例映射为结构化步骤（T5，故事 7/8/9/49）。

守护 UI → API → GLM CLI 子进程（映射 skill）→ 服务端字典二次校验 → DB
全链路（ADR-0001 结构化提取 / ADR-0002 未映射不猜测 / ADR-0006 子进程契约 /
ADR-0010 命令字典服务端校验）：

- 扩写闸门 sufficient/skipped 通过后才允许一键映射，异步可轮询
- 结构化步骤六字段落库可查看（字典命中夹具）
- LLM 自报合法的 mml_generic 输出落库前过字典二次校验，命令缺失/参数非法
  一律降级 unmapped（字典拦截夹具），并保留留痕
- CLI 超时/失败/坏输出落明确失败态，不动旧步骤；崩溃可恢复、可重试
- 重新映射整批替换旧步骤（故事 14）

fake 只注在系统边界（GLM CLI 可执行文件），子进程为真实进程。
"""
import json
import time
from pathlib import Path

import pytest

from app.config import settings
from app.db import SessionLocal
from app.mapping import (
    BadMappingResult,
    _new_job,
    build_catalog_subset,
    build_output_schema,
    reconcile_steps,
    validate_step_sequence,
)
from app.models import TextCase
from app.models.mapping import MappingStatus
from app.schemas import MappingCLIResult, MappingCLIStepIn

FIXTURE = Path(__file__).parent / "fixtures" / "fake_glm_cli.py"
CATALOG_DATA = Path(__file__).resolve().parents[1] / "app" / "catalog_data"

MAPPING_PATH = "/text-cases/{id}/mapping"
STEPS_PATH = "/text-cases/{id}/steps"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_cli(monkeypatch):
    """把后端 GLM CLI 指向 fake 可执行脚本；默认 10s 超时、unmapped 场景。"""
    monkeypatch.setattr(settings, "glm_cli_path", str(FIXTURE), raising=False)
    monkeypatch.setattr(settings, "glm_elaboration_skill", "elaboration-skill", raising=False)
    monkeypatch.setattr(settings, "glm_mapping_skill", "mapping-skill", raising=False)
    monkeypatch.setattr(settings, "glm_timeout_seconds", 10.0, raising=False)
    for var in ("FAKE_GLM_MAPPING", "FAKE_GLM_ELABORATION", "FAKE_GLM_SLEEP_SECONDS"):
        monkeypatch.delenv(var, raising=False)
    # slow 场景残留的 daemon 作业可能跨用例存活；清掉两个模块的进程内登记，
    # 避免清表后复用 case_id 时首触发被误判 409。
    from app import elaboration, mapping

    elaboration._running_jobs.clear()
    elaboration._active_procs.clear()
    mapping._running_jobs.clear()
    mapping._active_procs.clear()
    yield settings
    for var in ("FAKE_GLM_MAPPING", "FAKE_GLM_ELABORATION", "FAKE_GLM_SLEEP_SECONDS"):
        monkeypatch.delenv(var, raising=False)


def _set_mapping_scenario(monkeypatch, scenario: str):
    monkeypatch.setenv("FAKE_GLM_MAPPING", scenario)


def _create_case(client, title: str = "切换频段验证") -> dict:
    return client.post(
        "/text-cases",
        json={
            "title": title,
            "precondition": "基站已加电，BBU 与小区状态正常",
            "steps_text": "1. 激活目标小区\n2. 查询小区状态",
            "expected_text": "1. 小区状态为激活\n2. 返回小区状态",
        },
    ).json()


def _skip_gate(client, case_id: int):
    resp = client.post(f"/text-cases/{case_id}/elaboration/skip")
    assert resp.status_code == 200, resp.text


def _pass_gate_with_sufficient(client, case_id: int):
    resp = client.post(f"/text-cases/{case_id}/elaborate")
    assert resp.status_code == 202, resp.text


def _map(client, case_id: int):
    return client.post(f"/text-cases/{case_id}/map")


def _poll_mapping(client, case_id: int, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        resp = client.get(MAPPING_PATH.format(id=case_id))
        assert resp.status_code == 200
        body = resp.json()
        if body["state"] != "running":
            return body
        if time.monotonic() > deadline:
            raise AssertionError(f"mapping stuck in running: {body}")
        time.sleep(0.02)


def _steps(client, case_id: int) -> list:
    resp = client.get(STEPS_PATH.format(id=case_id))
    assert resp.status_code == 200
    return resp.json()


def _operation_id(client, name: str) -> int:
    entries = client.get("/operations").json()
    by_name = {entry["name"]: entry["id"] for entry in entries}
    assert name in by_name, f"演示操作目录缺少 {name}"
    return by_name[name]


def _import_demo_dictionary(client) -> dict:
    payload = json.loads((CATALOG_DATA / "command_dictionary.json").read_text(encoding="utf-8"))
    resp = client.post("/command-dictionaries/import", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# 纯函数：输出 schema 同源
# ---------------------------------------------------------------------------


def test_build_output_schema_is_single_source_with_validator():
    schema = build_output_schema()
    assert schema["$schema"].startswith("http://json-schema.org/")
    assert schema["additionalProperties"] is False
    step_schema = schema["$defs"]["MappingCLIStepIn"]
    assert step_schema["additionalProperties"] is False
    assert set(step_schema["properties"]["mapping_status"]["enum"]) == {"mapped", "unmapped"}
    assert step_schema["properties"]["seq"]["minimum"] == 1
    expected = MappingCLIResult.model_json_schema()
    assert {k: v for k, v in schema.items() if k != "$schema"} == expected


# ---------------------------------------------------------------------------
# 纯函数：候选集注入（条目携带 kind/device_target；mml_generic 挂字典片段）
# ---------------------------------------------------------------------------


def test_catalog_subset_entries_carry_kind_and_device_target(client):
    db = SessionLocal()
    try:
        subset = build_catalog_subset(db)
    finally:
        db.close()
    assert subset  # lifespan 已加载演示目录
    for entry in subset:
        assert entry["kind"] in (
            "mml_family",
            "mml_generic",
            "long_running",
            "instrument_primitive",
            "composite",
        )
        assert entry["device_target"] in ("bbu", "ue", "instrument", "mbb")
        assert isinstance(entry["id"], int) and entry["name"]


def test_catalog_subset_generic_has_empty_commands_when_dictionary_missing(client):
    db = SessionLocal()
    try:
        subset = build_catalog_subset(db)
        generic = next(e for e in subset if e["kind"] == "mml_generic")
    finally:
        db.close()
    # 字典缺失期注入空片段，但服务端校验仍是唯一闸门（见下方拦截测试）
    assert generic["commands"] == []
    assert generic["dictionary_version"] is None
    assert generic["dictionary_ref"] == "bbu-mml-dict"


def test_catalog_subset_generic_carries_active_dictionary_fragment(client):
    _import_demo_dictionary(client)
    db = SessionLocal()
    try:
        subset = build_catalog_subset(db)
        generic = next(e for e in subset if e["kind"] == "mml_generic")
    finally:
        db.close()
    assert generic["dictionary_version"] == "demo-bbu-v1.2"
    commands = {item["command"] for item in generic["commands"]}
    assert {"ACT_CELL", "MOD_CELL", "BLK_CELL", "DSP_CELL"} <= commands


# ---------------------------------------------------------------------------
# 纯函数：服务端对账（信任边界）
# ---------------------------------------------------------------------------


def _cli_step(seq=1, status="mapped", op_id=1, params=None, action="动作", assertion="断言"):
    return MappingCLIStepIn(
        seq=seq,
        action_text=action,
        aw_operation_id=op_id,
        params=params if params is not None else {},
        assertion_text=assertion,
        mapping_status=status,
    )


def test_validate_step_sequence_requires_contiguous_seq_from_one():
    validate_step_sequence([_cli_step(1), _cli_step(2)])  # 不抛
    with pytest.raises(BadMappingResult):
        validate_step_sequence([_cli_step(2)])
    with pytest.raises(BadMappingResult):
        validate_step_sequence([_cli_step(1), _cli_step(1)])
    with pytest.raises(BadMappingResult):
        validate_step_sequence([_cli_step(1), _cli_step(3)])


def test_reconcile_unmapped_step_keeps_unmapped_even_with_op_id(client):
    db = SessionLocal()
    try:
        rows, reclassified = reconcile_steps(db, [_cli_step(status="unmapped", op_id=999)])
    finally:
        db.close()
    assert rows[0][0] == MappingStatus.UNMAPPED
    assert rows[0][1] is None  # 夹带的操作引用被清空
    assert reclassified == []


def test_reconcile_mapped_without_operation_id_is_downgraded(client):
    db = SessionLocal()
    try:
        rows, reclassified = reconcile_steps(db, [_cli_step(op_id=None)])
    finally:
        db.close()
    assert rows[0][0] == MappingStatus.UNMAPPED
    assert reclassified[0].code == "missing_operation"


def test_reconcile_unknown_operation_id_is_downgraded(client):
    db = SessionLocal()
    try:
        rows, reclassified = reconcile_steps(db, [_cli_step(op_id=999999)])
    finally:
        db.close()
    assert rows[0][0] == MappingStatus.UNMAPPED
    assert rows[0][1] is None
    assert reclassified[0].code == "unknown_operation"


def test_reconcile_generic_mml_missing_dictionary_blocks_everything(client):
    """字典缺失期：LLM 自报合法的通用 MML 调用一律不放行。"""
    run_mml = _operation_id(client, "run_mml")
    db = SessionLocal()
    try:
        rows, reclassified = reconcile_steps(
            db,
            [_cli_step(op_id=run_mml, params={"command": "DSP_CELL", "args": {"cell_id": 1}})],
        )
    finally:
        db.close()
    assert rows[0][0] == MappingStatus.UNMAPPED
    assert reclassified[0].code == "dictionary_missing"


def test_reconcile_generic_mml_bad_params_shape_is_downgraded(client):
    _import_demo_dictionary(client)
    run_mml = _operation_id(client, "run_mml")
    db = SessionLocal()
    try:
        missing_command = reconcile_steps(
            db, [_cli_step(op_id=run_mml, params={"args": {}})]
        )
        assert missing_command[0][0][0] == MappingStatus.UNMAPPED
        assert missing_command[1][0].code == "bad_mml_params"

        args_not_object = reconcile_steps(
            db, [_cli_step(op_id=run_mml, params={"command": "DSP_CELL", "args": 1})]
        )
        assert args_not_object[0][0][0] == MappingStatus.UNMAPPED
        assert args_not_object[1][0].code == "bad_mml_params"
    finally:
        db.close()


def test_reconcile_mml_family_step_is_not_dictionary_checked(client):
    """非 mml_generic 操作不经过命令字典（高频命令族有自己的参数 schema）。"""
    _import_demo_dictionary(client)
    act_cell = _operation_id(client, "act_cell")
    db = SessionLocal()
    try:
        rows, reclassified = reconcile_steps(
            db, [_cli_step(op_id=act_cell, params={"cell_id": 1})]
        )
    finally:
        db.close()
    assert rows[0][0] == MappingStatus.MAPPED
    assert reclassified == []


# ---------------------------------------------------------------------------
# 触发准入：扩写闸门
# ---------------------------------------------------------------------------


def test_map_rejected_in_draft_without_gate(client, fake_cli):
    case = _create_case(client)
    resp = _map(client, case["id"])
    assert resp.status_code == 409
    assert "扩写闸门" in resp.json()["detail"]


def test_map_rejected_while_awaiting_elaboration_answers(client, fake_cli):
    case = _create_case(client)
    # 默认 insufficient 场景：扩写停在 awaiting_answers，闸门未通过
    _pass_gate_with_sufficient(client, case["id"])
    deadline = time.monotonic() + 5
    while client.get(f"/text-cases/{case['id']}/elaboration").json()["state"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.02)

    resp = _map(client, case["id"])
    assert resp.status_code == 409


def test_map_after_skip_returns_202_and_enters_running(client, fake_cli):
    case = _create_case(client)
    _skip_gate(client, case["id"])
    resp = _map(client, case["id"])
    assert resp.status_code == 202
    assert resp.json()["state"] == "running"
    # running 期间用例仍停留在 elaborating（不新增"映射中"用例状态）
    view = client.get(f"/text-cases/{case['id']}").json()
    assert view["status"] == "elaborating"
    assert view["mapping"]["state"] == "running"


def test_map_after_sufficient_gate_succeeds(client, fake_cli, monkeypatch):
    monkeypatch.setenv("FAKE_GLM_ELABORATION", "sufficient")
    case = _create_case(client)
    _pass_gate_with_sufficient(client, case["id"])
    deadline = time.monotonic() + 5
    while client.get(f"/text-cases/{case['id']}/elaboration").json()["state"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.02)

    assert _map(client, case["id"]).status_code == 202
    body = _poll_mapping(client, case["id"])
    assert body["state"] == "succeeded"


def test_map_unknown_case_returns_404(client, fake_cli):
    assert _map(client, 9999).status_code == 404


def test_get_mapping_before_start_returns_404(client, fake_cli):
    case = _create_case(client)
    assert client.get(MAPPING_PATH.format(id=case["id"])).status_code == 404


def test_get_steps_before_mapping_returns_empty_list(client, fake_cli):
    case = _create_case(client)
    assert _steps(client, case["id"]) == []


def test_duplicate_map_trigger_while_running_conflicts(client, fake_cli, monkeypatch):
    _set_mapping_scenario(monkeypatch, "slow")
    fake_cli.glm_timeout_seconds = 0.5
    case = _create_case(client)
    _skip_gate(client, case["id"])
    assert _map(client, case["id"]).status_code == 202
    assert _map(client, case["id"]).status_code == 409


# ---------------------------------------------------------------------------
# 全未映射夹具（默认场景）
# ---------------------------------------------------------------------------


def test_unmapped_steps_persisted_and_marked(client, fake_cli):
    case = _create_case(client)
    _skip_gate(client, case["id"])
    _map(client, case["id"])
    job = _poll_mapping(client, case["id"])

    assert job["state"] == "succeeded"
    assert job["step_count"] == 1
    assert job["mapped_count"] == 0
    assert job["unmapped_count"] == 1
    assert job["reclassifications"] == []

    view = client.get(f"/text-cases/{case['id']}").json()
    assert view["status"] == "mapped"

    steps = _steps(client, case["id"])
    assert len(steps) == 1
    step = steps[0]
    assert set(step.keys()) == {
        "id",
        "seq",
        "action_text",
        "aw_operation_id",
        "params",
        "assertion_text",
        "mapping_status",
    }
    assert step["seq"] == 1
    assert step["action_text"] == "激活目标小区"
    assert step["assertion_text"] == "小区状态为激活"
    assert step["aw_operation_id"] is None
    assert step["mapping_status"] == "unmapped"


# ---------------------------------------------------------------------------
# 字典命中夹具：mml_family + mml_generic 双 mapped 步骤落库
# ---------------------------------------------------------------------------


def test_matched_fixture_persists_mapped_steps_with_dictionary_hit(client, fake_cli, monkeypatch):
    _import_demo_dictionary(client)
    _set_mapping_scenario(monkeypatch, "matched")
    act_cell = _operation_id(client, "act_cell")
    run_mml = _operation_id(client, "run_mml")

    case = _create_case(client)
    _skip_gate(client, case["id"])
    _map(client, case["id"])
    job = _poll_mapping(client, case["id"])

    assert job["state"] == "succeeded"
    assert (job["step_count"], job["mapped_count"], job["unmapped_count"]) == (2, 2, 0)
    assert job["reclassifications"] == []

    steps = _steps(client, case["id"])
    assert [s["seq"] for s in steps] == [1, 2]
    assert steps[0]["mapping_status"] == "mapped"
    assert steps[0]["aw_operation_id"] == act_cell
    assert steps[0]["params"] == {"cell_id": 1}
    assert steps[0]["assertion_text"] == "小区状态为激活"
    assert steps[1]["mapping_status"] == "mapped"
    assert steps[1]["aw_operation_id"] == run_mml
    # 通用 MML 步骤落库 params 冻结 {command, args}（已过字典服务端校验）
    assert steps[1]["params"] == {"command": "DSP_CELL", "args": {"cell_id": 1}}
    assert steps[1]["action_text"]


# ---------------------------------------------------------------------------
# 字典拦截夹具：LLM 自报合法，服务端二次校验拦截 → unmapped
# ---------------------------------------------------------------------------


def test_intercepted_unknown_command_step_is_downgraded_to_unmapped(
    client, fake_cli, monkeypatch
):
    _import_demo_dictionary(client)
    _set_mapping_scenario(monkeypatch, "intercepted")
    run_mml = _operation_id(client, "run_mml")

    case = _create_case(client)
    _skip_gate(client, case["id"])
    _map(client, case["id"])
    job = _poll_mapping(client, case["id"])

    # 整体映射仍成功（部分拦截不拖垮整例，ADR-0002）；步骤被明确标记
    assert job["state"] == "succeeded"
    assert (job["step_count"], job["mapped_count"], job["unmapped_count"]) == (2, 1, 1)
    assert job["reclassifications"] == [
        {
            "seq": 2,
            "code": "unknown_command",
            "detail": "command LST_BOGUS not in dictionary",
        }
    ]

    steps = _steps(client, case["id"])
    assert steps[0]["mapping_status"] == "mapped"
    blocked = steps[1]
    assert blocked["mapping_status"] == "unmapped"
    assert blocked["aw_operation_id"] is None
    # run_mml 引用随降级移除；LLM 尝试的 command/args 保留在线索 params 中
    assert blocked["params"] == {"command": "LST_BOGUS", "args": {}}


def test_intercepted_out_of_range_param_step_is_downgraded(client, fake_cli, monkeypatch):
    _import_demo_dictionary(client)
    _set_mapping_scenario(monkeypatch, "bad_params")

    case = _create_case(client)
    _skip_gate(client, case["id"])
    _map(client, case["id"])
    job = _poll_mapping(client, case["id"])

    assert job["unmapped_count"] == 1
    reclass = job["reclassifications"][0]
    assert reclass["seq"] == 2
    assert reclass["code"] == "out_of_range"

    blocked = _steps(client, case["id"])[1]
    assert blocked["mapping_status"] == "unmapped"
    assert blocked["params"]["args"] == {"cell_id": 999999}


def test_generic_call_blocked_when_dictionary_not_imported(client, fake_cli, monkeypatch):
    """字典整体缺失：通用 MML 步骤（即使命令在演示字典里"看起来合法"）转 unmapped。"""
    _set_mapping_scenario(monkeypatch, "matched")  # 未导入字典
    case = _create_case(client)
    _skip_gate(client, case["id"])
    _map(client, case["id"])
    job = _poll_mapping(client, case["id"])

    assert job["mapped_count"] == 1
    assert job["unmapped_count"] == 1
    assert job["reclassifications"][0]["code"] == "dictionary_missing"
    assert _steps(client, case["id"])[1]["mapping_status"] == "unmapped"


def test_unknown_operation_self_report_is_downgraded(client, fake_cli, monkeypatch):
    _set_mapping_scenario(monkeypatch, "unknown_op")
    case = _create_case(client)
    _skip_gate(client, case["id"])
    _map(client, case["id"])
    job = _poll_mapping(client, case["id"])

    assert job["unmapped_count"] == 1
    assert job["reclassifications"][0]["code"] == "unknown_operation"
    step = _steps(client, case["id"])[0]
    assert step["mapping_status"] == "unmapped"
    assert step["aw_operation_id"] is None


# ---------------------------------------------------------------------------
# CLI 故障：明确失败态、不留脏数据、可重试
# ---------------------------------------------------------------------------


def test_cli_failure_marks_failed_without_steps(client, fake_cli, monkeypatch):
    _set_mapping_scenario(monkeypatch, "fail")
    case = _create_case(client)
    _skip_gate(client, case["id"])
    _map(client, case["id"])
    job = _poll_mapping(client, case["id"])

    assert job["state"] == "failed"
    assert job["error"]["code"] == "cli_failed"
    # 状态不推进、步骤不落库
    assert client.get(f"/text-cases/{case['id']}").json()["status"] == "elaborating"
    assert _steps(client, case["id"]) == []


def test_cli_malformed_result_marks_failed(client, fake_cli, monkeypatch):
    _set_mapping_scenario(monkeypatch, "malformed")
    case = _create_case(client)
    _skip_gate(client, case["id"])
    _map(client, case["id"])
    job = _poll_mapping(client, case["id"])
    assert job["state"] == "failed"
    assert job["error"]["code"] == "bad_result"


def test_cli_timeout_marks_failed_and_retry_succeeds(client, fake_cli, monkeypatch):
    _set_mapping_scenario(monkeypatch, "slow")
    monkeypatch.setenv("FAKE_GLM_SLEEP_SECONDS", "30")
    fake_cli.glm_timeout_seconds = 0.3
    case = _create_case(client)
    _skip_gate(client, case["id"])
    _map(client, case["id"])
    failed = _poll_mapping(client, case["id"], timeout=5.0)
    assert failed["state"] == "failed"
    assert failed["error"]["code"] == "timeout"

    # 失败后可重试：换 matched 场景 + 导入字典，作业正常完成
    _import_demo_dictionary(client)
    _set_mapping_scenario(monkeypatch, "matched")
    fake_cli.glm_timeout_seconds = 10.0
    assert _map(client, case["id"]).status_code == 202
    retried = _poll_mapping(client, case["id"])
    assert retried["state"] == "succeeded"
    assert retried["mapped_count"] == 2
    assert retried["error"] is None


def test_result_written_before_process_exit_is_consumed(client, fake_cli, monkeypatch):
    """ADR-0006：CLI 写完 result.json 后挂起，后端按文件取结果，不死等退出。"""
    _set_mapping_scenario(monkeypatch, "write_and_hang")
    monkeypatch.setenv("FAKE_GLM_SLEEP_SECONDS", "30")
    fake_cli.glm_timeout_seconds = 10.0
    case = _create_case(client)
    _skip_gate(client, case["id"])

    start = time.monotonic()
    _map(client, case["id"])
    job = _poll_mapping(client, case["id"], timeout=5.0)
    assert time.monotonic() - start < 3
    assert job["state"] == "succeeded"


def test_failed_remap_keeps_previous_steps_intact(client, fake_cli, monkeypatch):
    """重新映射失败不得破坏上一版已落库步骤（失败只落作业态）。"""
    _import_demo_dictionary(client)
    _set_mapping_scenario(monkeypatch, "matched")
    case = _create_case(client)
    _skip_gate(client, case["id"])
    _map(client, case["id"])
    assert _poll_mapping(client, case["id"])["mapped_count"] == 2
    original = _steps(client, case["id"])

    _set_mapping_scenario(monkeypatch, "fail")
    assert _map(client, case["id"]).status_code == 202
    failed = _poll_mapping(client, case["id"])
    assert failed["state"] == "failed"

    assert client.get(f"/text-cases/{case['id']}").json()["status"] == "mapped"
    assert _steps(client, case["id"]) == original


def test_successful_remap_replaces_old_steps(client, fake_cli, monkeypatch):
    """故事 14：重新映射产出新版本，旧结构化步骤整批作废。"""
    _import_demo_dictionary(client)
    _set_mapping_scenario(monkeypatch, "matched")
    case = _create_case(client)
    _skip_gate(client, case["id"])
    _map(client, case["id"])
    _poll_mapping(client, case["id"])
    assert len(_steps(client, case["id"])) == 2

    # 默认 unmapped 场景重新映射
    monkeypatch.delenv("FAKE_GLM_MAPPING", raising=False)
    assert _map(client, case["id"]).status_code == 202
    job = _poll_mapping(client, case["id"])
    assert job["step_count"] == 1
    new_steps = _steps(client, case["id"])
    assert len(new_steps) == 1
    assert new_steps[0]["mapping_status"] == "unmapped"


def test_patch_text_while_mapping_running_is_conflict(client, fake_cli, monkeypatch):
    _set_mapping_scenario(monkeypatch, "slow")
    fake_cli.glm_timeout_seconds = 1.0
    case = _create_case(client)
    _skip_gate(client, case["id"])
    _map(client, case["id"])
    assert client.get(MAPPING_PATH.format(id=case["id"])).json()["state"] == "running"

    resp = client.patch(f"/text-cases/{case['id']}", json={"steps_text": "改了"})
    assert resp.status_code == 409
    assert "映射" in resp.json()["detail"]


def test_editing_text_after_gate_passed_blocks_mapping_until_re_gated(client, fake_cli):
    """ADR-0007：闸门通过后改文本，闸门回退 answered——未评估文本不能流入映射。"""
    case = _create_case(client)
    _skip_gate(client, case["id"])
    patched = client.patch(
        f"/text-cases/{case['id']}", json={"steps_text": "1. 全新的步骤描述"}
    )
    assert patched.json()["elaboration"]["state"] == "answered"

    resp = _map(client, case["id"])
    assert resp.status_code == 409


def test_reap_interrupted_mapping_jobs_recovers_stale_running(client, fake_cli):
    """崩溃恢复：启动后残留 running 的映射作业落 failed/interrupted，可重新触发。"""
    from app.mapping import reap_interrupted_jobs

    case = _create_case(client)
    _skip_gate(client, case["id"])
    db = SessionLocal()
    try:
        row = db.get(TextCase, case["id"])
        row.mapping_job = _new_job("running", job_token="dead-token")
        db.commit()
    finally:
        db.close()

    reaper_db = SessionLocal()
    try:
        assert reap_interrupted_jobs(reaper_db) == 1
    finally:
        reaper_db.close()

    body = client.get(MAPPING_PATH.format(id=case["id"])).json()
    assert body["state"] == "failed"
    assert body["error"]["code"] == "interrupted"
    assert _map(client, case["id"]).status_code == 202
    _poll_mapping(client, case["id"])


def test_case_view_embeds_mapping_job_summary(client, fake_cli):
    case = _create_case(client)
    _skip_gate(client, case["id"])
    _map(client, case["id"])
    _poll_mapping(client, case["id"])

    view = client.get(f"/text-cases/{case['id']}").json()
    assert view["mapping"]["state"] == "succeeded"
    assert view["mapping"]["unmapped_count"] == 1
    # 内部令牌不得泄漏到对外视图
    assert "job_token" not in view["mapping"]
