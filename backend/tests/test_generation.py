"""系统级测试：模板渲染可执行用例（T7，故事 12–15）。

守护 UI → POST /text-cases/{id}/generate → executable_case 版本链全链路：

- 生成闸门：仅 confirmed/generated 态可生成，其余 409
- 版本只追加不覆盖：改文本/确认态内容后重新映射/生成产生新版本，历史可回溯
- 代码全文只读查看；系统不存在任何代码编辑入口（404/405 守护）
- 渲染失败（缺参数）返回 422 且不产生空版本
"""
import json
import time
from pathlib import Path

import pytest

from app.config import settings

FIXTURE = Path(__file__).parent / "fixtures" / "fake_glm_cli.py"
CATALOG_DATA = Path(__file__).resolve().parents[1] / "app" / "catalog_data"

GENERATE_PATH = "/text-cases/{id}/generate"
VERSIONS_PATH = "/text-cases/{id}/executable-cases"
CODE_PATH = "/executable-cases/{id}/code"


# ---------------------------------------------------------------------------
# 夹具与助手（与 test_confirmation 同一套全链路驱动方式）
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


def _import_demo_dictionary(client) -> None:
    payload = json.loads((CATALOG_DATA / "command_dictionary.json").read_text(encoding="utf-8"))
    resp = client.post("/command-dictionaries/import", json=payload)
    assert resp.status_code == 201, resp.text


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


def _make_confirmed_case(client, fake_cli, monkeypatch) -> dict:
    """产出 confirmed 态用例：两步均 mapped（mml_family + mml_generic）。"""
    _import_demo_dictionary(client)
    monkeypatch.setenv("FAKE_GLM_MAPPING", "matched")
    case = _create_case(client)
    assert client.post(f"/text-cases/{case['id']}/elaboration/skip").status_code == 200
    assert client.post(f"/text-cases/{case['id']}/map").status_code == 202
    job = _poll_mapping(client, case["id"])
    assert job["unmapped_count"] == 0, job
    resp = client.post(f"/text-cases/{case['id']}/confirm")
    assert resp.status_code == 200, resp.text
    return case


def _versions(client, case_id: int) -> list:
    resp = client.get(VERSIONS_PATH.format(id=case_id))
    assert resp.status_code == 200
    return resp.json()


def _code(client, exec_id: int) -> str:
    resp = client.get(CODE_PATH.format(id=exec_id))
    assert resp.status_code == 200
    return resp.json()["code"]


# ---------------------------------------------------------------------------
# 生成闸门与首次生成
# ---------------------------------------------------------------------------


def test_generate_requires_confirmed_or_generated(client, fake_cli, monkeypatch):
    """draft/mapped 态直接拒绝生成（409），确认前不产出任何版本。"""
    _import_demo_dictionary(client)
    monkeypatch.setenv("FAKE_GLM_MAPPING", "matched")
    case = _create_case(client)
    resp = client.post(GENERATE_PATH.format(id=case["id"]))
    assert resp.status_code == 409

    assert client.post(f"/text-cases/{case['id']}/elaboration/skip").status_code == 200
    assert client.post(f"/text-cases/{case['id']}/map").status_code == 202
    _poll_mapping(client, case["id"])
    resp = client.post(GENERATE_PATH.format(id=case["id"]))
    assert resp.status_code == 409
    assert _versions(client, case["id"]) == []


def test_generate_unknown_case_404(client):
    assert client.post(GENERATE_PATH.format(id=9999)).status_code == 404
    assert client.get(VERSIONS_PATH.format(id=9999)).status_code == 404


def test_generate_first_version_and_view_code(client, fake_cli, monkeypatch):
    """AC 1/2：确认后生成 v1，状态转 generated；代码全文含 Allure step 标记。"""
    case = _make_confirmed_case(client, fake_cli, monkeypatch)

    resp = client.post(GENERATE_PATH.format(id=case["id"]))
    assert resp.status_code == 201, resp.text
    first = resp.json()
    assert first["version"] == 1
    assert first["text_case_id"] == case["id"]

    assert client.get(f"/text-cases/{case['id']}").json()["status"] == "generated"

    versions = _versions(client, case["id"])
    assert [v["version"] for v in versions] == [1]

    code = _code(client, first["id"])
    assert "import allure" in code
    assert "allure.step" in code
    assert "步骤1: 激活目标小区" in code
    assert "aw.bbu.act_cell(cell_id=1)" in code
    assert "run_mml(command='DSP_CELL'" in code
    assert "禁止手工编辑" in code
    compile(code, "<generated>", "exec")


# ---------------------------------------------------------------------------
# 版本只追加不覆盖（AC 2/4）
# ---------------------------------------------------------------------------


def test_regenerate_within_generated_state_appends_version(client, fake_cli, monkeypatch):
    """generated 态内重新生成：不产生状态迁移，版本递增，历史保留。"""
    case = _make_confirmed_case(client, fake_cli, monkeypatch)
    first = client.post(GENERATE_PATH.format(id=case["id"])).json()
    second = client.post(GENERATE_PATH.format(id=case["id"])).json()

    assert second["version"] == 2
    assert client.get(f"/text-cases/{case['id']}").json()["status"] == "generated"
    assert [v["version"] for v in _versions(client, case["id"])] == [2, 1]
    assert _code(client, first["id"]) == _code(client, second["id"])


def test_regenerate_after_upstream_edit_keeps_history(client, fake_cli, monkeypatch):
    """AC 4：改文本 + 改确认态内容 → 重新映射/生成 → 新版本，旧版本不覆盖。

    闭环：v1 → PATCH 文本（回退 elaborating）→ 重跳扩写 → 重映射 →
    PATCH 步骤参数（cell_id 1→2）→ 确认 → 生成 v2 → v1/v2 代码均可见且不同。
    """
    case = _make_confirmed_case(client, fake_cli, monkeypatch)
    first = client.post(GENERATE_PATH.format(id=case["id"])).json()
    code_v1 = _code(client, first["id"])
    assert "cell_id=1" in code_v1

    # 修改文本：mapped/confirmed/generated 态编辑评估输入 → 回退 elaborating
    resp = client.patch(
        f"/text-cases/{case['id']}", json={"steps_text": "1. 激活目标小区\n2. 查询小区状态\n3. 复核"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "elaborating"

    # 重跳扩写闸门 → 重新映射（仍是 matched 场景）
    monkeypatch.setenv("FAKE_GLM_MAPPING", "matched")
    assert client.post(f"/text-cases/{case['id']}/elaboration/skip").status_code == 200
    assert client.post(f"/text-cases/{case['id']}/map").status_code == 202
    assert _poll_mapping(client, case["id"])["unmapped_count"] == 0

    # 确认态内容修改：步骤 1 参数 cell_id 1 → 2（仅改参数保留 mapped）
    steps = client.get(f"/text-cases/{case['id']}/steps").json()
    resp = client.patch(
        f"/text-cases/{case['id']}/steps",
        json={"steps": [{"id": steps[0]["id"], "params": {"cell_id": 2}}]},
    )
    assert resp.status_code == 200, resp.text

    resp = client.post(f"/text-cases/{case['id']}/confirm")
    assert resp.status_code == 200, resp.text
    second = client.post(GENERATE_PATH.format(id=case["id"])).json()

    assert second["version"] == 2
    assert [v["version"] for v in _versions(client, case["id"])] == [2, 1]
    code_v2 = _code(client, second["id"])
    assert "cell_id=2" in code_v2
    assert code_v2 != code_v1
    assert _code(client, first["id"]) == code_v1  # 旧版本原文未动


# ---------------------------------------------------------------------------
# 生成闸门防御与渲染失败（AC 5）
# ---------------------------------------------------------------------------


def _tamper_step(step_id: int, *, params=None, status=None, op_id=None) -> None:
    """直接改库构造防御分支（正常流程到不了这里，闸门仍须守住）。"""
    from app.db import SessionLocal
    from app.models import StructuredStep

    db = SessionLocal()
    try:
        row = db.get(StructuredStep, step_id)
        if params is not None:
            row.params = params
        if status is not None:
            row.mapping_status = status
        if op_id is not None:
            row.aw_operation_id = op_id
        db.commit()
    finally:
        db.close()


def test_generate_rejected_when_step_unmapped(client, fake_cli, monkeypatch):
    """存在未映射步骤时生成 409（确认闸门的运行期防御）。"""
    case = _make_confirmed_case(client, fake_cli, monkeypatch)
    steps = client.get(f"/text-cases/{case['id']}/steps").json()
    _tamper_step(steps[0]["id"], status="unmapped", op_id=None)

    resp = client.post(GENERATE_PATH.format(id=case["id"]))
    assert resp.status_code == 409
    assert "步骤 1" in resp.json()["detail"]
    assert _versions(client, case["id"]) == []


def test_render_failure_returns_422_and_no_empty_version(client, fake_cli, monkeypatch):
    """AC 5：缺必填参数渲染失败 → 422 携带步骤序号与原因，不产生空版本。"""
    case = _make_confirmed_case(client, fake_cli, monkeypatch)
    steps = client.get(f"/text-cases/{case['id']}/steps").json()
    _tamper_step(steps[0]["id"], params={})

    resp = client.post(GENERATE_PATH.format(id=case["id"]))
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "missing_required_params"
    assert detail["step_seq"] == 1
    assert "cell_id" in detail["detail"]

    assert _versions(client, case["id"]) == []
    assert client.get(f"/text-cases/{case['id']}").json()["status"] == "confirmed"


# ---------------------------------------------------------------------------
# 只读边界（AC 3）：系统不存在任何代码编辑入口
# ---------------------------------------------------------------------------


def test_code_view_has_no_edit_endpoint(client, fake_cli, monkeypatch):
    """对已存在路径换写方法 → 405；未注册路径 → 404。代码只读不可编辑。"""
    case = _make_confirmed_case(client, fake_cli, monkeypatch)
    first = client.post(GENERATE_PATH.format(id=case["id"])).json()
    code_path = CODE_PATH.format(id=first["id"])

    assert client.put(code_path, json={"code": "x"}).status_code == 405
    assert client.patch(code_path, json={"code": "x"}).status_code == 405
    assert client.delete(code_path).status_code == 405
    assert client.post(code_path, json={"code": "x"}).status_code == 405
    # 整个可执行用例资源同样没有任何写入口
    exec_path = f"/executable-cases/{first['id']}"
    assert client.patch(exec_path, json={"code": "x"}).status_code == 404
    assert client.delete(exec_path).status_code == 404


def test_code_view_unknown_executable_404(client):
    assert client.get(CODE_PATH.format(id=9999)).status_code == 404
