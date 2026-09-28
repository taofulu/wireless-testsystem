"""系统级测试：步骤级报告与五环追溯（T13，Issue #14，故事 33/34/37/47）。

守护的验收标准：
- Allure 解析后每个执行步骤映射回原始文本步骤序号，失败点可秒级定位（故事 33）
- 同一用例多次真实执行历史可对比、识别 flaky；调试会话不出现在正式历史中
  （故事 37，trace 的 executions 环只含 real）
- 五环追溯视图可双向回查（非进化路径种子/进化环为空），仅统计真实执行（故事 34）
- 沙盒报告显著区分于真实执行报告（仿真标注、未环境校验声明，故事 47）
- 报告/追溯查询行为经系统级测试守护（本文件）
"""
from app.db import SessionLocal
from app.reporting import identify_flaky, parse_allure_steps, parse_step_seq

from tests.test_execution import _claim, _make_executable_case, _register
from tests.test_real_execution import _execute_ready, _submit


# ---------------------------------------------------------------------------
# 纯函数单元测试：Allure 解析与 flaky 识别的边界守护
# ---------------------------------------------------------------------------


def test_parse_step_seq_extracts_text_step_number():
    """Allure step 标题 → 原始文本步骤序号；容忍空白与全角冒号。"""
    assert parse_step_seq("步骤1: 激活目标小区") == 1
    assert parse_step_seq("步骤 2 ：查询状态") == 2
    # 非步骤锚点（setup/teardown 附属步骤）返回 None
    assert parse_step_seq("setup") is None
    assert parse_step_seq("") is None


def test_parse_allure_steps_maps_to_seq_and_action_text():
    """Allure 结果原文 → 逐步骤映射回原始文本步骤序号 + 动作文本回填。"""
    action_by_seq = {1: "激活小区", 2: "查询状态"}
    allure = {
        "steps": [
            {"title": "步骤1: 激活小区", "status": "passed"},
            {"title": "步骤2: 查询状态", "status": "failed"},
            {"title": "teardown", "status": "passed"},  # 非步骤锚点
        ]
    }
    parsed = parse_allure_steps(allure, action_by_seq)
    assert len(parsed) == 3
    assert parsed[0].seq == 1 and parsed[0].action_text == "激活小区"
    assert parsed[1].seq == 2 and parsed[1].status == "failed"  # 失败点定位
    assert parsed[2].seq is None  # 附属步骤不映射


def test_parse_allure_steps_defensive_against_malformed_report():
    """Allure 原文形状不可控：None/缺 steps/坏条目一律不抛错。"""
    assert parse_allure_steps(None, {}) == []
    assert parse_allure_steps({}, {}) == []
    assert parse_allure_steps({"steps": "not-a-list"}, {}) == []
    # 缺 title/status 的条目跳过
    assert parse_allure_steps({"steps": [{"title": "步骤1: x"}]}, {}) == []
    assert parse_allure_steps({"steps": [{"status": "passed"}]}, {}) == []


def test_identify_flaky_edge_cases():
    """flaky 识别：单次无法判定；同判决不 flaky；passed+failed（断言）即 flaky。

    env_failed 是环境类失败（故事 53），不计为代码 flaky——passed+env_failed
    不是 flaky（环境不稳 ≠ 代码 flaky），引导工程师查环境而非查代码。
    """
    assert identify_flaky([]).is_flaky is False  # 0 次
    assert identify_flaky(["passed"]).is_flaky is False  # 单次
    assert identify_flaky(["passed", "passed"]).is_flaky is False  # 全过
    flaky = identify_flaky(["passed", "failed"])
    assert flaky.is_flaky is True and flaky.failed_count == 1
    # env_failed 不计为代码 flaky：passed+env_failed 引导查环境而非查代码
    env_only = identify_flaky(["passed", "env_failed"])
    assert env_only.is_flaky is False
    assert env_only.failed_count == 0  # env_failed 不进 failed_count
    # 但 failed + env_failed + passed 仍 flaky（存在断言失败）
    assert identify_flaky(["passed", "failed", "env_failed"]).is_flaky is True



def test_step_report_maps_allure_steps_to_text_step_seq(client, fake_cli, fake_lass, monkeypatch):
    """Allure 报告逐步骤映射回原始文本步骤序号，失败点定位到第 2 步。"""
    made = _make_executable_case(client, fake_cli, monkeypatch)
    exec_id = made["executable"]["id"]
    task = _execute_ready(client, exec_id)
    _register(client, "real-01", ("real",))
    assert _claim(client, "real-01", ("real",)).json()["task_id"] == task["id"]

    # 用例两步（激活目标小区 / 动态查询小区状态）；第 2 步断言失败
    allure_report = {
        "steps": [
            {"title": "步骤1: 激活目标小区", "status": "passed"},
            {"title": "步骤2: 动态查询小区状态（通用 MML）", "status": "failed"},
        ]
    }
    resp = _submit(
        client, task["id"], "real-01", verdict="failed", logs="step2 failed",
        allure_report=allure_report,
    )
    assert resp.status_code == 201, resp.text

    report = client.get(
        f"/executable-cases/{exec_id}/executions/{task['id']}/report"
    ).json()
    assert report["execution_target"] == "real"
    assert report["is_sandbox"] is False
    assert report["verdict"] == "failed"
    # real 报告不带沙盒声明（故事 47：显著区分）
    assert report["environment_disclaimer"] is None
    # 逐步骤映射回原始文本步骤序号
    assert len(report["steps"]) == 2
    assert report["steps"][0]["seq"] == 1
    assert report["steps"][0]["status"] == "passed"
    assert report["steps"][0]["action_text"] == "激活目标小区"
    assert report["steps"][1]["seq"] == 2
    assert report["steps"][1]["status"] == "failed"  # 失败点秒级定位
    assert report["steps"][1]["action_text"] == "动态查询小区状态（通用 MML）"


def test_step_report_pending_execution_returns_empty_steps(
    client, fake_cli, fake_lass, monkeypatch
):
    """未回传结果的任务：步骤级报告呈现空步骤（前端据此显示执行中）。"""
    made = _make_executable_case(client, fake_cli, monkeypatch)
    exec_id = made["executable"]["id"]
    task = _execute_ready(client, exec_id)
    _register(client, "real-01", ("real",))
    _claim(client, "real-01", ("real",))  # 领取但未回传

    report = client.get(
        f"/executable-cases/{exec_id}/executions/{task['id']}/report"
    ).json()
    assert report["verdict"] == ""
    assert report["steps"] == []


def test_step_report_task_not_belonging_to_case_404(
    client, fake_cli, fake_lass, monkeypatch
):
    """任务不属于该可执行用例：404，报告不跨用例泄漏。"""
    made = _make_executable_case(client, fake_cli, monkeypatch)
    exec_id = made["executable"]["id"]
    # 用一个不存在的 task_id
    assert (
        client.get(f"/executable-cases/{exec_id}/executions/999999/report").status_code
        == 404
    )


# ---------------------------------------------------------------------------
# 五环追溯：非进化路径种子/进化环为空（故事 34）
# ---------------------------------------------------------------------------


def test_trace_non_evolution_path_seed_evolution_rings_empty(
    client, fake_cli, fake_lass, monkeypatch
):
    """非进化路径（parent_case_id 为 null）：种子/进化两环为空，其余三环有值。"""
    made = _make_executable_case(client, fake_cli, monkeypatch)
    exec_id = made["executable"]["id"]

    trace = client.get(f"/executable-cases/{exec_id}/trace").json()
    assert trace["executable_case_id"] == exec_id
    # 种子/进化环为空（手写用例，非进化路径）
    assert trace["seed_case"] is None
    assert trace["evolution_case"] is None
    # 结构化环、代码环有值
    assert len(trace["structured_steps"]) == 2
    assert trace["structured_steps"][0]["seq"] == 1
    assert trace["executable_case"]["id"] == exec_id
    assert trace["executable_case"]["version"] == 1
    # 结果环：尚无真实执行
    assert trace["executions"]["records"] == []
    assert trace["executions"]["flaky"]["total_runs"] == 0
    assert trace["executions"]["flaky"]["is_flaky"] is False


def test_trace_evolution_path_seed_and_evolution_rings_populated(
    client, fake_cli, fake_lass, monkeypatch
):
    """进化路径：parent_case_id 指向种子用例，种子/进化两环可双向回查。"""
    made = _make_executable_case(client, fake_cli, monkeypatch)
    exec_id = made["executable"]["id"]
    case_id = made["case"]["id"]

    # 手工把当前用例挂到一条种子用例下（T14 的进化 API 未落地，直接落库）
    db = SessionLocal()
    try:
        from app.models import TextCase

        seed = TextCase(title="种子用例", precondition="", steps_text="", expected_text="")
        db.add(seed)
        db.commit()
        db.refresh(seed)
        cur = db.get(TextCase, case_id)
        assert cur is not None
        cur.parent_case_id = seed.id
        db.commit()
        seed_id = seed.id
    finally:
        db.close()

    trace = client.get(f"/executable-cases/{exec_id}/trace").json()
    # 双向回查：进化环=当前用例，种子环=父用例
    assert trace["evolution_case"]["id"] == case_id
    assert trace["seed_case"]["id"] == seed_id
    assert trace["seed_case"]["title"] == "种子用例"


# ---------------------------------------------------------------------------
# 多次真实执行历史 + flaky 识别（故事 37）
# ---------------------------------------------------------------------------


def test_trace_flaky_identification_across_multiple_real_runs(
    client, fake_cli, fake_lass, monkeypatch
):
    """同一用例多次真实执行历史对比，passed+failed 即 flaky（故事 37）。"""
    made = _make_executable_case(client, fake_cli, monkeypatch)
    exec_id = made["executable"]["id"]
    case_id = made["case"]["id"]

    # 第一次：passed
    task1 = _execute_ready(client, exec_id)
    _register(client, "real-01", ("real",))
    _claim(client, "real-01", ("real",))
    _submit(
        client, task1["id"], "real-01", verdict="passed", logs="ok",
        allure_report={"steps": [{"title": "步骤1: 激活目标小区", "status": "passed"}]},
    )
    assert client.get(f"/text-cases/{case_id}").json()["status"] == "done"

    # 环境阻断后复检可新开第二条 real 任务；这里直接再走 execute 闸门
    # （done 态用例需先回 generated——简化：直接落第二条 real 任务并回传 failed）
    from app.models.execution import ExecutionTask

    db = SessionLocal()
    try:
        t2 = ExecutionTask(
            executable_case_id=exec_id, execution_target="real", status="queued"
        )
        db.add(t2)
        db.commit()
        db.refresh(t2)
        t2_id = t2.id
    finally:
        db.close()

    _register(client, "real-02", ("real",))
    _claim(client, "real-02", ("real",))
    _submit(
        client, t2_id, "real-02", verdict="failed", logs="flaky fail",
        allure_report={"steps": [{"title": "步骤1: 激活目标小区", "status": "failed"}]},
    )

    trace = client.get(f"/executable-cases/{exec_id}/trace").json()
    ring = trace["executions"]
    assert len(ring["records"]) == 2
    # 新的在前（id desc）
    assert [r["id"] for r in ring["records"]] == [t2_id, task1["id"]]
    flaky = ring["flaky"]
    assert flaky["total_runs"] == 2
    assert flaky["passed_count"] == 1
    assert flaky["failed_count"] == 1
    assert flaky["is_flaky"] is True


def test_trace_debug_sessions_excluded_from_official_history(
    client, fake_cli, fake_lass, monkeypatch
):
    """沙盒调试会话不进五环追溯的 executions 环（ADR-0009，故事 37）。"""
    made = _make_executable_case(client, fake_cli, monkeypatch)
    exec_id = made["executable"]["id"]

    # 发起沙盒调试（不入正式历史/追溯）
    assert client.post(f"/executable-cases/{exec_id}/debug").status_code == 201

    trace = client.get(f"/executable-cases/{exec_id}/trace").json()
    assert trace["executions"]["records"] == []  # 沙盒调试不进追溯
    assert trace["executions"]["flaky"]["total_runs"] == 0


# ---------------------------------------------------------------------------
# 沙盒报告显著区分于真实执行报告（故事 47）
# ---------------------------------------------------------------------------


def test_sandbox_step_report_carries_environment_disclaimer(
    client, fake_cli, fake_lass, monkeypatch
):
    """沙盒任务步骤报告携带 environment_disclaimer，与真实报告显著区分。"""
    made = _make_executable_case(client, fake_cli, monkeypatch)
    exec_id = made["executable"]["id"]

    # 发起沙盒调试并回传
    debug_task = client.post(f"/executable-cases/{exec_id}/debug").json()
    _register(client, "sandbox-01", ("sandbox",))
    assert _claim(client, "sandbox-01", ("sandbox",)).json()["task_id"] == debug_task["id"]
    _submit(
        client, debug_task["id"], "sandbox-01", verdict="passed", logs="sim ok",
        step_results=[{"seq": 1, "sim_level": "simulated", "status": "pass"}],
    )

    report = client.get(
        f"/executable-cases/{exec_id}/executions/{debug_task['id']}/report"
    ).json()
    assert report["is_sandbox"] is True
    assert report["execution_target"] == "sandbox"
    # 沙盒报告显著标注：仿真执行未进行环境校验（故事 47）
    assert report["environment_disclaimer"] == "仿真执行、未进行环境校验"
    assert len(report["steps"]) == 1
    assert report["steps"][0]["seq"] == 1
    assert report["steps"][0]["action_text"] == "激活目标小区"


def test_real_report_no_disclaimer_distinguishes_from_sandbox(
    client, fake_cli, fake_lass, monkeypatch
):
    """真实执行报告不带沙盒声明，与沙盒报告视觉分流（故事 47）。"""
    made = _make_executable_case(client, fake_cli, monkeypatch)
    exec_id = made["executable"]["id"]
    task = _execute_ready(client, exec_id)
    _register(client, "real-01", ("real",))
    _claim(client, "real-01", ("real",))
    _submit(
        client, task["id"], "real-01", verdict="passed", logs="ok",
        allure_report={"steps": [{"title": "步骤1: 激活目标小区", "status": "passed"}]},
    )

    report = client.get(
        f"/executable-cases/{exec_id}/executions/{task['id']}/report"
    ).json()
    assert report["is_sandbox"] is False
    assert report["environment_disclaimer"] is None  # 真实报告无沙盒声明
