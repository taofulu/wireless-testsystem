"""系统级测试：确认态人工审核（T6，故事 8–12、51、55）。

守护 UI → PATCH /text-cases/{id}/steps（编辑操作/参数）→ POST /confirm
（全映射后确认）全链路（ADR-0002 未映射不猜测 / ADR-0010 命令字典校验）：

- 未映射步骤手选操作 → 状态变 manual
- 已映射步骤可改操作（变 manual）或仅改参数（保留 mapped）
- mml_generic 手选同样过命令字典服务端二次校验，命令缺失/参数非法一律拒绝
- 场景类操作（play_scenario）参数冻结 scenario_id+scenario_version；
  无场景索引时手工录入 ID+版本可保存
- 确认闸门：存在 unmapped 步骤或操作引用为空时拒绝（409），全部映射后进入 confirmed
"""
import json
import time
from pathlib import Path

import pytest

from app.config import settings

FIXTURE = Path(__file__).parent / "fixtures" / "fake_glm_cli.py"
CATALOG_DATA = Path(__file__).resolve().parents[1] / "app" / "catalog_data"

STEPS_PATH = "/text-cases/{id}/steps"
CONFIRM_PATH = "/text-cases/{id}/confirm"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_cli(monkeypatch):
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
    assert client.post(f"/text-cases/{case_id}/elaboration/skip").status_code == 200


def _map(client, case_id: int):
    return client.post(f"/text-cases/{case_id}/map")


def _poll_mapping(client, case_id: int, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        resp = client.get(f"/text-cases/{case_id}/mapping")
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


def _make_mapped_case(client, fake_cli, monkeypatch, scenario="matched") -> dict:
    """产出一个 mapped 态用例（默认 matched：两步均 mapped）。"""
    if scenario == "matched":
        _import_demo_dictionary(client)
    _set_mapping_scenario(monkeypatch, scenario)
    case = _create_case(client)
    _skip_gate(client, case["id"])
    _map(client, case["id"])
    _poll_mapping(client, case["id"])
    return case


# ---------------------------------------------------------------------------
# AC 1/2：未映射步骤展示与手选变 manual
# ---------------------------------------------------------------------------


def test_patch_steps_rejected_when_not_in_mapped_state(client, fake_cli, monkeypatch):
    """确认态编辑只允许 mapped 态用例（draft/elaborating/confirmed 均拒绝）。"""
    case = _create_case(client)  # draft
    resp = client.patch(
        STEPS_PATH.format(id=case["id"]),
        json={"steps": [{"id": 1, "params": {}}]},
    )
    assert resp.status_code == 409


def test_handpick_unmapped_step_becomes_manual(client, fake_cli, monkeypatch):
    """AC 2：未映射步骤手选操作后状态变 manual。"""
    case = _make_mapped_case(client, fake_cli, monkeypatch, scenario="unmapped")
    steps = _steps(client, case["id"])
    assert steps[0]["mapping_status"] == "unmapped"

    act_cell = _operation_id(client, "act_cell")
    resp = client.patch(
        STEPS_PATH.format(id=case["id"]),
        json={
            "steps": [
                {
                    "id": steps[0]["id"],
                    "aw_operation_id": act_cell,
                    "params": {"cell_id": 1},
                }
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated[0]["mapping_status"] == "manual"
    assert updated[0]["aw_operation_id"] == act_cell
    assert updated[0]["params"] == {"cell_id": 1}


# ---------------------------------------------------------------------------
# AC 3：编辑已映射步骤的操作与参数
# ---------------------------------------------------------------------------


def test_edit_mapped_step_params_keeps_mapped_status(client, fake_cli, monkeypatch):
    """AC 3：仅改参数且操作不变时保留 mapped 状态（LLM 选的操作仍可追溯）。"""
    case = _make_mapped_case(client, fake_cli, monkeypatch, scenario="matched")
    steps = _steps(client, case["id"])
    step1 = steps[0]
    assert step1["mapping_status"] == "mapped"

    resp = client.patch(
        STEPS_PATH.format(id=case["id"]),
        json={"steps": [{"id": step1["id"], "params": {"cell_id": 42}}]},
    )
    assert resp.status_code == 200
    updated = resp.json()[0]
    assert updated["mapping_status"] == "mapped"
    assert updated["aw_operation_id"] == step1["aw_operation_id"]
    assert updated["params"] == {"cell_id": 42}


def test_change_mapped_step_operation_becomes_manual(client, fake_cli, monkeypatch):
    """AC 3：改已映射步骤的操作 → 人工干预 → manual。"""
    case = _make_mapped_case(client, fake_cli, monkeypatch, scenario="matched")
    steps = _steps(client, case["id"])
    step1 = steps[0]
    set_power = _operation_id(client, "set_rf_power")

    resp = client.patch(
        STEPS_PATH.format(id=case["id"]),
        json={
            "steps": [
                {"id": step1["id"], "aw_operation_id": set_power, "params": {"power_dbm": -10}}
            ]
        },
    )
    assert resp.status_code == 200
    updated = resp.json()[0]
    assert updated["mapping_status"] == "manual"
    assert updated["aw_operation_id"] == set_power


def test_clear_operation_reverts_to_unmapped(client, fake_cli, monkeypatch):
    """手选后可撤销：aw_operation_id 显式 null → 清空操作、状态回 unmapped。"""
    case = _make_mapped_case(client, fake_cli, monkeypatch, scenario="matched")
    steps = _steps(client, case["id"])
    step1 = steps[0]

    resp = client.patch(
        STEPS_PATH.format(id=case["id"]),
        json={"steps": [{"id": step1["id"], "aw_operation_id": None, "params": {}}]},
    )
    assert resp.status_code == 200
    updated = resp.json()[0]
    assert updated["mapping_status"] == "unmapped"
    assert updated["aw_operation_id"] is None


# ---------------------------------------------------------------------------
# 信任边界：操作存在性 + mml_generic 字典校验
# ---------------------------------------------------------------------------


def test_patch_unknown_operation_id_is_rejected(client, fake_cli, monkeypatch):
    case = _make_mapped_case(client, fake_cli, monkeypatch, scenario="unmapped")
    steps = _steps(client, case["id"])
    resp = client.patch(
        STEPS_PATH.format(id=case["id"]),
        json={"steps": [{"id": steps[0]["id"], "aw_operation_id": 999999, "params": {}}]},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "unknown_operation"


def test_patch_mml_generic_without_dictionary_is_rejected(client, fake_cli, monkeypatch):
    """人工手选 mml_generic 同样过命令字典：字典缺失期一律拒绝。"""
    case = _make_mapped_case(client, fake_cli, monkeypatch, scenario="unmapped")
    steps = _steps(client, case["id"])
    run_mml = _operation_id(client, "run_mml")

    resp = client.patch(
        STEPS_PATH.format(id=case["id"]),
        json={
            "steps": [
                {
                    "id": steps[0]["id"],
                    "aw_operation_id": run_mml,
                    "params": {"command": "DSP_CELL", "args": {"cell_id": 1}},
                }
            ]
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "dictionary_missing"


def test_patch_mml_generic_unknown_command_is_rejected(client, fake_cli, monkeypatch):
    _import_demo_dictionary(client)
    case = _make_mapped_case(client, fake_cli, monkeypatch, scenario="unmapped")
    steps = _steps(client, case["id"])
    run_mml = _operation_id(client, "run_mml")

    resp = client.patch(
        STEPS_PATH.format(id=case["id"]),
        json={
            "steps": [
                {
                    "id": steps[0]["id"],
                    "aw_operation_id": run_mml,
                    "params": {"command": "LST_BOGUS", "args": {}},
                }
            ]
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "unknown_command"


def test_patch_mml_generic_valid_command_succeeds(client, fake_cli, monkeypatch):
    _import_demo_dictionary(client)
    case = _make_mapped_case(client, fake_cli, monkeypatch, scenario="unmapped")
    steps = _steps(client, case["id"])
    run_mml = _operation_id(client, "run_mml")

    resp = client.patch(
        STEPS_PATH.format(id=case["id"]),
        json={
            "steps": [
                {
                    "id": steps[0]["id"],
                    "aw_operation_id": run_mml,
                    "params": {"command": "DSP_CELL", "args": {"cell_id": 1}},
                }
            ]
        },
    )
    assert resp.status_code == 200
    assert resp.json()[0]["mapping_status"] == "manual"


# ---------------------------------------------------------------------------
# AC 4：场景类操作参数冻结 scenario_id+version，手工录入可保存
# ---------------------------------------------------------------------------


def test_patch_scenario_operation_requires_scenario_id_and_version(
    client, fake_cli, monkeypatch
):
    """场景类操作（play_scenario composite）params 必须冻结 scenario_id+version。"""
    case = _make_mapped_case(client, fake_cli, monkeypatch, scenario="unmapped")
    steps = _steps(client, case["id"])
    play = _operation_id(client, "play_scenario")

    # 缺 scenario_version
    resp = client.patch(
        STEPS_PATH.format(id=case["id"]),
        json={
            "steps": [
                {"id": steps[0]["id"], "aw_operation_id": play, "params": {"scenario_id": "S1"}}
            ]
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "bad_scenario_params"


def test_patch_scenario_operation_manual_entry_saved_without_index(
    client, fake_cli, monkeypatch
):
    """AC 4：无场景索引时手工录入 scenario_id+version 可保存（不校验索引存在性）。"""
    # 未导入任何场景索引（/scenarios 为空）
    assert client.get("/scenarios").json() == []

    case = _make_mapped_case(client, fake_cli, monkeypatch, scenario="unmapped")
    steps = _steps(client, case["id"])
    play = _operation_id(client, "play_scenario")

    resp = client.patch(
        STEPS_PATH.format(id=case["id"]),
        json={
            "steps": [
                {
                    "id": steps[0]["id"],
                    "aw_operation_id": play,
                    "params": {"scenario_id": "SC-001", "scenario_version": "v1"},
                }
            ]
        },
    )
    assert resp.status_code == 200
    updated = resp.json()[0]
    assert updated["mapping_status"] == "manual"
    assert updated["params"] == {"scenario_id": "SC-001", "scenario_version": "v1"}


def test_operations_endpoint_groups_by_kind_and_device_target(client):
    """AC 2：操作目录条目携带 kind/device_target，供前端按维度组织下拉。"""
    ops = client.get("/operations").json()
    assert ops
    kinds = {op["kind"] for op in ops}
    targets = {op["device_target"] for op in ops}
    assert {"mml_family", "mml_generic", "long_running", "instrument_primitive", "composite"} <= kinds
    assert {"bbu", "instrument"} <= targets
    # 每个条目都有 params_schema 与 simulatable（供确认态参数编辑与仿真可用性展示）
    for op in ops:
        assert "params_schema" in op
        assert op["simulatable"] in ("schema_stub", "declarative", "python", "none")


# ---------------------------------------------------------------------------
# AC 5：确认闸门（存在未映射步骤时禁止确认；全部映射后进入 confirmed）
# ---------------------------------------------------------------------------


def test_confirm_rejected_with_unmapped_steps(client, fake_cli, monkeypatch):
    """AC 5：存在 unmapped 步骤时禁止确认（API 闸门被测试守护）。"""
    case = _make_mapped_case(client, fake_cli, monkeypatch, scenario="unmapped")
    resp = client.post(CONFIRM_PATH.format(id=case["id"]))
    assert resp.status_code == 409
    assert "未映射" in resp.json()["detail"]
    # 状态不变
    assert client.get(f"/text-cases/{case['id']}").json()["status"] == "mapped"


def test_confirm_succeeds_when_all_steps_mapped(client, fake_cli, monkeypatch):
    """AC 5：全部步骤 mapped 后确认成功，进入 confirmed 态。"""
    case = _make_mapped_case(client, fake_cli, monkeypatch, scenario="matched")
    steps = _steps(client, case["id"])
    assert all(s["mapping_status"] == "mapped" for s in steps)

    resp = client.post(CONFIRM_PATH.format(id=case["id"]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "confirmed"
    assert body["step_count"] == 2
    assert body["manual_count"] == 0

    view = client.get(f"/text-cases/{case['id']}").json()
    assert view["status"] == "confirmed"


def test_confirm_succeeds_with_manual_steps(client, fake_cli, monkeypatch):
    """AC 5：manual 步骤也算已映射，可确认。"""
    case = _make_mapped_case(client, fake_cli, monkeypatch, scenario="unmapped")
    steps = _steps(client, case["id"])
    act_cell = _operation_id(client, "act_cell")
    client.patch(
        STEPS_PATH.format(id=case["id"]),
        json={
            "steps": [
                {"id": steps[0]["id"], "aw_operation_id": act_cell, "params": {"cell_id": 1}}
            ]
        },
    )

    resp = client.post(CONFIRM_PATH.format(id=case["id"]))
    assert resp.status_code == 200
    assert resp.json()["manual_count"] == 1
    assert client.get(f"/text-cases/{case['id']}").json()["status"] == "confirmed"


def test_confirm_rejected_when_not_in_mapped_state(client, fake_cli, monkeypatch):
    case = _create_case(client)  # draft
    resp = client.post(CONFIRM_PATH.format(id=case["id"]))
    assert resp.status_code == 409


def test_confirm_rejected_when_already_confirmed(client, fake_cli, monkeypatch):
    """已 confirmed 用例再次确认返回 409（状态机不允许回退/重复确认）。"""
    case = _make_mapped_case(client, fake_cli, monkeypatch, scenario="matched")
    assert client.post(CONFIRM_PATH.format(id=case["id"])).status_code == 200
    resp = client.post(CONFIRM_PATH.format(id=case["id"]))
    assert resp.status_code == 409


def test_patch_steps_rejected_after_confirm(client, fake_cli, monkeypatch):
    """confirmed 态用例不能再编辑步骤（已确认的映射不可改，需重新映射）。"""
    case = _make_mapped_case(client, fake_cli, monkeypatch, scenario="matched")
    client.post(CONFIRM_PATH.format(id=case["id"]))
    steps = _steps(client, case["id"])
    resp = client.patch(
        STEPS_PATH.format(id=case["id"]),
        json={"steps": [{"id": steps[0]["id"], "params": {"cell_id": 99}}]},
    )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# 批量编辑原子性：任一步失败整批拒绝
# ---------------------------------------------------------------------------


def test_batch_patch_is_atomic_on_failure(client, fake_cli, monkeypatch):
    """批量编辑中任一步校验失败，整批回滚，不落半更新。"""
    case = _make_mapped_case(client, fake_cli, monkeypatch, scenario="matched")
    steps = _steps(client, case["id"])
    original_params = steps[0]["params"]

    # 第一步合法改参数，第二步引用不存在操作
    resp = client.patch(
        STEPS_PATH.format(id=case["id"]),
        json={
            "steps": [
                {"id": steps[0]["id"], "params": {"cell_id": 7}},
                {"id": steps[1]["id"], "aw_operation_id": 999999, "params": {}},
            ]
        },
    )
    assert resp.status_code == 422
    # 第一步的修改未持久化
    assert _steps(client, case["id"])[0]["params"] == original_params


def test_patch_step_not_belonging_to_case_is_rejected(client, fake_cli, monkeypatch):
    case1 = _make_mapped_case(client, fake_cli, monkeypatch, scenario="matched")
    case2 = _make_mapped_case(client, fake_cli, monkeypatch, scenario="unmapped")
    steps1 = _steps(client, case1["id"])

    resp = client.patch(
        STEPS_PATH.format(id=case2["id"]),
        json={"steps": [{"id": steps1[0]["id"], "params": {"cell_id": 1}}]},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "step_not_found"


# ---------------------------------------------------------------------------
# 必填参数校验（非 mml_generic 操作）
# ---------------------------------------------------------------------------


def test_patch_missing_required_params_is_rejected(client, fake_cli, monkeypatch):
    """非 mml_generic 操作按 params_schema 必填字段校验（act_cell 需要 cell_id）。"""
    case = _make_mapped_case(client, fake_cli, monkeypatch, scenario="unmapped")
    steps = _steps(client, case["id"])
    act_cell = _operation_id(client, "act_cell")

    resp = client.patch(
        STEPS_PATH.format(id=case["id"]),
        json={"steps": [{"id": steps[0]["id"], "aw_operation_id": act_cell, "params": {}}]},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "missing_required_params"
