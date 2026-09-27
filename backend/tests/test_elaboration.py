"""系统级测试：扩写问答闭环（T4，故事 3–6 / ADR-0006 / ADR-0007）。

守护 UI → API → GLM CLI 子进程 → DB 全链路：
- 触发扩写后进入 elaborating，fake CLI 异步产出可轮询
- sufficient=false 展示 missing_points（field+question）
- 逐条回答合并回原文形成新版本，可多轮迭代
- 强制跳过入口，跳过后允许进入映射
- CLI 超时/失败/坏输出回退明确失败态，不动原文、不产脏数据

fake 只注在系统边界（GLM CLI 可执行文件），子进程为真实进程。
"""
import time
from pathlib import Path

import pytest

from app.elaboration import (
    build_output_schema,
    merge_answers,
    render_input_md,
)
from app.schemas import ElaborationCLIResult

FIXTURE = Path(__file__).parent / "fixtures" / "fake_glm_cli.py"

ELABORATION_PATH = "/text-cases/{id}/elaboration"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_cli(monkeypatch):
    """把后端 GLM CLI 指向 fake 可执行脚本；默认 10s 超时、insufficient 场景。"""
    from app.config import settings

    monkeypatch.setattr(settings, "glm_cli_path", str(FIXTURE), raising=False)
    monkeypatch.setattr(settings, "glm_elaboration_skill", "elaboration-skill", raising=False)
    monkeypatch.setattr(settings, "glm_timeout_seconds", 10.0, raising=False)
    monkeypatch.delenv("FAKE_GLM_ELABORATION", raising=False)
    monkeypatch.delenv("FAKE_GLM_SLEEP_SECONDS", raising=False)
    # 上一用例若以 slow 场景收尾，其 daemon 作业可能仍在跑；内存表清表后
    # case_id 会从 1 复用，清掉残留登记以免下一用例首触发被误判 409。
    from app import elaboration

    elaboration._running_jobs.clear()
    elaboration._active_procs.clear()
    yield settings
    monkeypatch.delenv("FAKE_GLM_ELABORATION", raising=False)
    monkeypatch.delenv("FAKE_GLM_SLEEP_SECONDS", raising=False)


def _set_scenario(monkeypatch, scenario: str):
    monkeypatch.setenv("FAKE_GLM_ELABORATION", scenario)


def _create_case(client, title: str = "切换频段验证") -> dict:
    return client.post(
        "/text-cases",
        json={
            "title": title,
            "precondition": "基站已加电，BBU 与小区状态正常",
            "steps_text": "1. 查询当前频段\n2. 将频段切换为 D8\n3. 复查频段",
            "expected_text": "1. 返回当前频段\n2. 命令成功\n3. 频段为 D8",
        },
    ).json()


def _poll(client, case_id: int, terminal_only=("running",), timeout: float = 5.0) -> dict:
    """轮询扩写状态直到离开 running（默认）。"""
    deadline = time.monotonic() + timeout
    while True:
        resp = client.get(ELABORATION_PATH.format(id=case_id))
        assert resp.status_code == 200
        body = resp.json()
        if body["state"] not in terminal_only:
            return body
        if time.monotonic() > deadline:
            raise AssertionError(f"elaboration stuck in running: {body}")
        time.sleep(0.02)


def _start(client, case_id: int):
    return client.post(f"/text-cases/{case_id}/elaborate")


# ---------------------------------------------------------------------------
# 纯函数：input.md 渲染与答案合并
# ---------------------------------------------------------------------------


def test_render_input_md_contains_three_sections():
    md = render_input_md(
        {
            "title": "用例 X",
            "precondition": "预知条件文本",
            "steps_text": "步骤文本",
            "expected_text": "预期文本",
        }
    )
    assert "# 用例：用例 X" in md
    assert "## 预知条件" in md and "预知条件文本" in md
    assert "## 测试步骤" in md and "步骤文本" in md
    assert "## 预期结果" in md and "预期文本" in md


def test_merge_answers_appends_answer_under_named_field():
    current = {"precondition": "原始预知条件", "steps_text": "原始步骤", "expected_text": ""}
    merged = merge_answers(
        current,
        [{"field": "precondition", "question": "UE 是否注册？", "answer": "UE 已注册并附着"}],
    )
    assert merged["precondition"].startswith("原始预知条件")
    assert "UE 已注册并附着" in merged["precondition"]
    assert "UE 是否注册？" in merged["precondition"]
    # 其他字段不受影响
    assert merged["steps_text"] == "原始步骤"
    # 空字段合并后不留前导空行
    assert merged["expected_text"] == ""


def test_merge_answers_supports_multiple_fields_and_empty_originals():
    merged = merge_answers(
        {"precondition": "", "steps_text": "", "expected_text": ""},
        [
            {"field": "precondition", "question": "Q1", "answer": "A1"},
            {"field": "steps_text", "question": "Q2", "answer": "A2"},
            {"field": "steps_text", "question": "Q3", "answer": "A3"},
        ],
    )
    pre_lines = [line for line in merged["precondition"].splitlines() if line.strip()]
    step_lines = [line for line in merged["steps_text"].splitlines() if line.strip()]
    assert len(pre_lines) == 1 and "A1" in pre_lines[0]
    assert len(step_lines) == 2
    assert all("A" in line for line in step_lines)


# ---------------------------------------------------------------------------
# 触发与轮询
# ---------------------------------------------------------------------------


def test_elaborate_returns_202_and_enters_elaborating(client, fake_cli):
    case = _create_case(client)
    resp = _start(client, case["id"])
    assert resp.status_code == 202
    body = resp.json()
    assert body["state"] == "running"
    assert body["round"] == 1

    case_view = client.get(f"/text-cases/{case['id']}").json()
    assert case_view["status"] == "elaborating"
    assert case_view["elaboration"]["state"] == "running"


def test_poll_returns_missing_points_when_insufficient(client, fake_cli):
    case = _create_case(client)
    _start(client, case["id"])

    body = _poll(client, case["id"])
    assert body["state"] == "awaiting_answers"
    assert body["round"] == 1
    point = body["missing_points"][0]
    assert set(point.keys()) == {"field", "question"}
    assert point["field"] == "precondition"
    assert point["question"]  # 非空追问
    # 当轮问答进入历史，答案待提交
    assert body["rounds"][0]["round"] == 1
    assert body["rounds"][0]["missing_points"] == body["missing_points"]
    assert body["rounds"][0]["answers"] == []
    assert body["error"] is None


def test_get_elaboration_before_start_returns_404(client, fake_cli):
    case = _create_case(client)
    resp = client.get(ELABORATION_PATH.format(id=case["id"]))
    assert resp.status_code == 404


def test_get_elaboration_unknown_case_returns_404(client, fake_cli):
    assert client.get(ELABORATION_PATH.format(id=9999)).status_code == 404


def test_elaborate_rejects_duplicate_trigger_while_running(client, fake_cli, monkeypatch):
    _set_scenario(monkeypatch, "slow")
    fake_cli.glm_timeout_seconds = 0.5  # 守护线程在断言后尽快自行了断，不滞留
    case = _create_case(client)
    assert _start(client, case["id"]).status_code == 202
    # 评估进行中重复触发：冲突，不创建第二个作业
    resp = _start(client, case["id"])
    assert resp.status_code == 409


def test_elaborate_unknown_case_returns_404(client, fake_cli):
    assert _start(client, 9999).status_code == 404


# ---------------------------------------------------------------------------
# 回答合并与多轮迭代
# ---------------------------------------------------------------------------


def _answer_current(client, case_id: int, elaboration: dict, texts=None) -> dict:
    if texts is None:
        texts = [f"答案：{point['question']}" for point in elaboration["missing_points"]]
    answers = [
        {"field": point["field"], "question": point["question"], "answer": text}
        for point, text in zip(elaboration["missing_points"], texts)
    ]
    resp = client.post(ELABORATION_PATH.format(id=case_id) + "/answers", json={"answers": answers})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_answers_merge_into_new_version(client, fake_cli):
    case = _create_case(client)
    _start(client, case["id"])
    pending = _poll(client, case["id"])

    updated = _answer_current(client, case["id"], pending, texts=["UE 已注册并附着目标小区"])
    assert updated["status"] == "elaborating"
    qa = updated["elaboration"]
    assert qa["state"] == "answered"
    assert qa["missing_points"] == []
    # 原文形成新版本：答案合并回对应字段
    assert "UE 已注册并附着目标小区" in updated["precondition"]
    assert "基站已加电" in updated["precondition"]  # 原文保留
    assert qa["rounds"][0]["answers"][0]["answer"] == "UE 已注册并附着目标小区"

    persisted = client.get(f"/text-cases/{case['id']}").json()
    assert "UE 已注册并附着目标小区" in persisted["precondition"]


def test_re_elaborate_after_answers_starts_round_two(client, fake_cli, monkeypatch):
    case = _create_case(client)
    _start(client, case["id"])
    pending = _poll(client, case["id"])
    _answer_current(client, case["id"], pending)

    # 第二轮仍判不足（同一 CLI 对合并后版本再次评估）
    resp = _start(client, case["id"])
    assert resp.status_code == 202
    body = _poll(client, case["id"])
    assert body["round"] == 2
    assert body["state"] == "awaiting_answers"
    # 第一轮历史保留（多轮可追溯）
    assert body["rounds"][0]["round"] == 1
    assert body["rounds"][0]["answers"]
    assert body["rounds"][1]["round"] == 2


def test_answers_only_allowed_when_awaiting_answers(client, fake_cli):
    case = _create_case(client)
    # 未触发扩写直接回答 → 409
    resp = client.post(
        ELABORATION_PATH.format(id=case["id"]) + "/answers",
        json={"answers": [{"field": "precondition", "question": "Q", "answer": "A"}]},
    )
    assert resp.status_code == 409


def test_answers_must_cover_every_current_missing_point(client, fake_cli):
    case = _create_case(client)
    _start(client, case["id"])
    pending = _poll(client, case["id"])
    # 空答案列表拒绝
    resp = client.post(
        ELABORATION_PATH.format(id=case["id"]) + "/answers", json={"answers": []}
    )
    assert resp.status_code == 422

    # 漏答一条（fake 当前只有一条，改交一个不属于本轮的问题）同样拒绝
    point = pending["missing_points"][0]
    stale = [{"field": point["field"], "question": "已过期的其他追问", "answer": "答非所问"}]
    resp = client.post(
        ELABORATION_PATH.format(id=case["id"]) + "/answers", json={"answers": stale}
    )
    assert resp.status_code == 422

    # 空白答案拒绝
    blank = [{"field": point["field"], "question": point["question"], "answer": "   "}]
    resp = client.post(
        ELABORATION_PATH.format(id=case["id"]) + "/answers", json={"answers": blank}
    )
    assert resp.status_code == 422


def test_full_multi_round_chain_until_sufficient(client, fake_cli, monkeypatch):
    """全链路：录入 → 不足 → 回答合并 → 再评估 → sufficient（故事 3–5）。"""
    case = _create_case(client)
    _start(client, case["id"])
    round_one = _poll(client, case["id"])
    assert round_one["state"] == "awaiting_answers"

    _answer_current(client, case["id"], round_one, texts=["UE 已注册并附着到目标小区"])

    # 同一份用例补全后，CLI 判定充分
    _set_scenario(monkeypatch, "sufficient")
    _start(client, case["id"])
    done = _poll(client, case["id"])
    assert done["state"] == "sufficient"
    assert done["missing_points"] == []
    assert done["round"] == 2

    final = client.get(f"/text-cases/{case['id']}").json()
    assert final["status"] == "elaborating"
    assert final["elaboration"]["state"] == "sufficient"
    assert "UE 已注册并附着到目标小区" in final["precondition"]


def test_result_written_before_process_exit_is_consumed(client, fake_cli, monkeypatch):
    """ADR-0006：后端轮询 result.json 存在性——CLI 写完挂起也能取结果，

    不必死等进程退出，也不应误判超时。
    """
    _set_scenario(monkeypatch, "write_and_hang")
    monkeypatch.setenv("FAKE_GLM_SLEEP_SECONDS", "30")
    fake_cli.glm_timeout_seconds = 10.0
    case = _create_case(client)

    start = time.monotonic()
    _start(client, case["id"])
    body = _poll(client, case["id"], timeout=5.0)
    assert time.monotonic() - start < 3  # 远早于 30s 挂起与 10s 超时
    assert body["state"] == "sufficient"


def test_concurrent_elaborate_triggers_admit_only_one(client, fake_cli, monkeypatch):
    """并发双击：进程内准入保证同一用例只有一个作业（一个 202、一个 409）。"""
    import threading

    _set_scenario(monkeypatch, "slow")
    fake_cli.glm_timeout_seconds = 0.5
    case = _create_case(client)

    barrier = threading.Barrier(2)
    results: list[int] = []

    def trigger():
        barrier.wait()
        results.append(_start(client, case["id"]).status_code)

    threads = [threading.Thread(target=trigger) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [202, 409]


def test_elaborate_after_completion_is_conflict(client, fake_cli, monkeypatch):
    case = _create_case(client)
    _set_scenario(monkeypatch, "sufficient")
    _start(client, case["id"])
    _poll(client, case["id"])

    # 扩写闸门已通过，不允许重新评估（下一步应进入映射）
    assert _start(client, case["id"]).status_code == 409


# ---------------------------------------------------------------------------
# 强制跳过
# ---------------------------------------------------------------------------


def test_skip_directly_from_draft_without_cli(client, fake_cli):
    """故事 6：紧急场景不评估，直接跳过扩写，允许进入映射。"""
    case = _create_case(client)
    resp = client.post(ELABORATION_PATH.format(id=case["id"]) + "/skip")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "elaborating"
    assert body["elaboration"]["state"] == "skipped"
    # 原文不被改动
    assert body["precondition"] == "基站已加电，BBU 与小区状态正常"


def test_skip_from_awaiting_answers(client, fake_cli):
    case = _create_case(client)
    _start(client, case["id"])
    _poll(client, case["id"])

    resp = client.post(ELABORATION_PATH.format(id=case["id"]) + "/skip")
    assert resp.status_code == 200
    assert resp.json()["elaboration"]["state"] == "skipped"


def test_skip_after_sufficient_is_conflict(client, fake_cli, monkeypatch):
    """已通过的闸门不能被 skip 降格（后端守护，即使 UI 不暴露入口）。"""
    _set_scenario(monkeypatch, "sufficient")
    case = _create_case(client)
    _start(client, case["id"])
    _poll(client, case["id"])

    resp = client.post(ELABORATION_PATH.format(id=case["id"]) + "/skip")
    assert resp.status_code == 409


def test_editing_text_after_gate_passed_invalidates_gate(client, fake_cli, monkeypatch):
    """ADR-0007：sufficient 后改动评估输入（含标题/三栏），闸门失效回 answered，

    必须重新扩写或再次跳过，映射不能消费未评估的新文本。
    """
    _set_scenario(monkeypatch, "sufficient")
    case = _create_case(client)
    _start(client, case["id"])
    _poll(client, case["id"])

    patched = client.patch(
        f"/text-cases/{case['id']}", json={"steps_text": "1. 查询功率\n2. 设置为 43dBm"}
    )
    assert patched.status_code == 200
    qa = patched.json()["elaboration"]
    assert qa["state"] == "answered"
    # 多轮历史保留
    assert qa["rounds"]

    # 回退态允许重新触发扩写
    assert _start(client, case["id"]).status_code == 202
    _poll(client, case["id"])


def test_editing_text_while_awaiting_answers_cancels_stale_questions(client, fake_cli):
    """awaiting 态改原文：旧追问针对旧文本，作废追问回 answered，不留脱节数据。"""
    case = _create_case(client)
    _start(client, case["id"])
    body = _poll(client, case["id"])
    assert body["state"] == "awaiting_answers"
    assert body["missing_points"]

    patched = client.patch(
        f"/text-cases/{case['id']}", json={"steps_text": "1. 全新的步骤描述"}
    )
    qa = patched.json()["elaboration"]
    assert qa["state"] == "answered"
    assert qa["missing_points"] == []


def test_same_value_patch_does_not_invalidate_gate(client, fake_cli, monkeypatch):
    """值未变化（即使字段出现在 PATCH 里）不应作废闸门。"""
    _set_scenario(monkeypatch, "sufficient")
    case = _create_case(client)
    _start(client, case["id"])
    _poll(client, case["id"])

    resp = client.patch(
        f"/text-cases/{case['id']}", json={"steps_text": case["steps_text"]}
    )
    assert resp.json()["elaboration"]["state"] == "sufficient"


def test_reap_interrupted_jobs_recovers_stale_running(client, fake_cli):
    """崩溃恢复：启动后残留的 running（无活线程回写）落 failed/interrupted，

    用例不再被永久封死，可重新触发。
    """
    from app.db import SessionLocal
    from app.elaboration import _new_qa, reap_interrupted_jobs
    from app.models import TextCase, TextCaseStatus

    case = _create_case(client)
    db = SessionLocal()
    try:
        row = db.get(TextCase, case["id"])
        row.status = TextCaseStatus.ELABORATING
        row.elaboration_qa = _new_qa(1, "running", job_token="dead-token")
        db.commit()
    finally:
        db.close()

    reaper_db = SessionLocal()
    try:
        assert reap_interrupted_jobs(reaper_db) == 1
    finally:
        reaper_db.close()

    body = client.get(ELABORATION_PATH.format(id=case["id"])).json()
    assert body["state"] == "failed"
    assert body["error"]["code"] == "interrupted"
    # failed 态允许重试，不再 409 封死
    assert _start(client, case["id"]).status_code == 202


def test_build_output_schema_is_single_source_with_validator():
    """注入 CLI 的 output_schema.json 必须就是后端验收模型的 JSON schema，

    钉死关键边界（额外字段拒绝、question 长度、field 枚举），防双份漂移。
    """
    schema = build_output_schema()
    assert schema["$schema"].startswith("http://json-schema.org/")
    assert schema["additionalProperties"] is False
    missing = schema["$defs"]["MissingPoint"]
    assert missing["additionalProperties"] is False
    assert missing["properties"]["question"]["maxLength"] == 500
    assert set(missing["properties"]["field"]["enum"]) == {
        "precondition",
        "steps_text",
        "expected_text",
    }
    # 直接与 pydantic 导出结果同源（$schema 键除外，那是注入时补的）
    expected = ElaborationCLIResult.model_json_schema()
    assert {k: v for k, v in schema.items() if k != "$schema"} == expected


def test_editing_after_skip_also_invalidates_gate(client, fake_cli):
    case = _create_case(client)
    client.post(ELABORATION_PATH.format(id=case["id"]) + "/skip")

    patched = client.patch(f"/text-cases/{case['id']}", json={"title": "改了标题的用例"})
    assert patched.json()["elaboration"]["state"] == "answered"


def test_patch_while_running_is_conflict(client, fake_cli, monkeypatch):
    """评估进行中改文本会造成注入输入与结论脱节，PATCH 必须被挡下。"""
    _set_scenario(monkeypatch, "slow")
    fake_cli.glm_timeout_seconds = 1.0
    case = _create_case(client)
    _start(client, case["id"])
    assert (
        client.get(ELABORATION_PATH.format(id=case["id"])).json()["state"] == "running"
    )

    resp = client.patch(f"/text-cases/{case['id']}", json={"steps_text": "改了"})
    assert resp.status_code == 409


def test_empty_patch_leaves_completed_gate_intact(client, fake_cli, monkeypatch):
    """空 PATCH 不携带任何字段，不算改动评估输入，闸门保持 sufficient。"""
    _set_scenario(monkeypatch, "sufficient")
    case = _create_case(client)
    _start(client, case["id"])
    _poll(client, case["id"])

    resp = client.patch(f"/text-cases/{case['id']}", json={})
    assert resp.json()["elaboration"]["state"] == "sufficient"


def test_skip_while_running_is_conflict(client, fake_cli, monkeypatch):
    _set_scenario(monkeypatch, "slow")
    fake_cli.glm_timeout_seconds = 0.5  # 守护线程在断言后尽快自行了断，不滞留
    case = _create_case(client)
    _start(client, case["id"])
    # 进行中不允许跳过，避免作业线程回写与跳过互相覆盖
    resp = client.post(ELABORATION_PATH.format(id=case["id"]) + "/skip")
    assert resp.status_code == 409


def test_skip_unknown_case_returns_404(client, fake_cli):
    resp = client.post(ELABORATION_PATH.format(id=9999) + "/skip")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# CLI 超时 / 失败 / 坏输出：明确失败态，不产生脏数据
# ---------------------------------------------------------------------------


def test_cli_nonzero_exit_marks_failed_without_dirty_data(client, fake_cli, monkeypatch):
    _set_scenario(monkeypatch, "fail")
    case = _create_case(client)
    original = client.get(f"/text-cases/{case['id']}").json()

    _start(client, case["id"])
    body = _poll(client, case["id"])
    assert body["state"] == "failed"
    assert body["error"]["code"] == "cli_failed"
    assert body["error"]["detail"]

    after = client.get(f"/text-cases/{case['id']}").json()
    assert after["precondition"] == original["precondition"]
    assert after["steps_text"] == original["steps_text"]
    assert after["expected_text"] == original["expected_text"]
    assert after["elaboration"]["missing_points"] == []


def test_cli_timeout_marks_failed_and_allows_retry(client, fake_cli, monkeypatch):
    _set_scenario(monkeypatch, "slow")
    monkeypatch.setenv("FAKE_GLM_SLEEP_SECONDS", "30")
    fake_cli.glm_timeout_seconds = 0.3
    case = _create_case(client)

    _start(client, case["id"])
    body = _poll(client, case["id"], timeout=5.0)
    assert body["state"] == "failed"
    assert body["error"]["code"] == "timeout"

    # 失败后可重新触发：恢复 sufficient 场景，作业正常完成
    _set_scenario(monkeypatch, "sufficient")
    fake_cli.glm_timeout_seconds = 10.0
    assert _start(client, case["id"]).status_code == 202
    retried = _poll(client, case["id"])
    assert retried["state"] == "sufficient"
    assert retried["error"] is None


def test_cli_malformed_result_marks_failed(client, fake_cli, monkeypatch):
    _set_scenario(monkeypatch, "malformed")
    case = _create_case(client)
    _start(client, case["id"])
    body = _poll(client, case["id"])
    assert body["state"] == "failed"
    assert body["error"]["code"] == "bad_result"


def test_answers_after_failure_remain_conflict(client, fake_cli, monkeypatch):
    """失败态不接受答案（没有可信 missing_points），只能重试或跳过。"""
    _set_scenario(monkeypatch, "fail")
    case = _create_case(client)
    _start(client, case["id"])
    _poll(client, case["id"])

    resp = client.post(
        ELABORATION_PATH.format(id=case["id"]) + "/answers",
        json={"answers": [{"field": "precondition", "question": "Q", "answer": "A"}]},
    )
    assert resp.status_code == 409


def test_skip_after_failure_allowed(client, fake_cli, monkeypatch):
    _set_scenario(monkeypatch, "fail")
    case = _create_case(client)
    _start(client, case["id"])
    _poll(client, case["id"])

    resp = client.post(ELABORATION_PATH.format(id=case["id"]) + "/skip")
    assert resp.status_code == 200
    assert resp.json()["elaboration"]["state"] == "skipped"
