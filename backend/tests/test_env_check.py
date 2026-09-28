"""系统级测试：LASS 三值环境校验闸门（T11，Issue #12，故事 16-22、44）。

守护的验收标准：
- execute 触发 LASS 校验（fake LASS 夹具）：ready 任务入队；needs_create
  回缺失资源清单；needs_modify 回差异说明
- 阻断路径用例转 done 且任务体带 env_check_result/env_check_detail（前端
  经同一 API 呈现原因与下一步——TestClient 全链路即 UI→API→DB 的接缝）
- recheck 在环境处理后可重新校验，ready 后正常入队
- 沙盒 inconclusive 的用例未带确认标记时拒绝入队（且不打到 LASS）
- 三种环境结果各有端到端用例
"""
from app.config import settings

from tests.test_execution import (
    _claim,
    _debug_task,
    _make_executable_case,
    _register,
)

TOPOLOGY = {"bbu": 1, "ue": 2, "instrument": ["rf-power-meter"]}

# fake_cli 夹具统一由 conftest 提供（系统边界 fake，ADR-0006）


def _sandbox_verdict(client, exec_id: int, verdict: str, step_results: list) -> None:
    """跑一轮沙盒调试并回传指定判决（驱动 T10 闸门到目标状态）。"""
    task = _debug_task(client, exec_id)
    _register(client, "sandbox-01", ("sandbox",))
    _claim(client, "sandbox-01")
    resp = client.post(
        f"/worker/tasks/{task['id']}/result",
        json={
            "worker_id": "sandbox-01",
            "verdict": verdict,
            "logs": "ok",
            "step_results": step_results,
        },
    )
    assert resp.status_code == 201, resp.text


def _sandbox_passed(client, exec_id: int) -> None:
    _sandbox_verdict(
        client, exec_id, "passed",
        [{"seq": 1, "sim_level": "simulated", "status": "pass"}],
    )


# ---------------------------------------------------------------------------
# 故事 16：所需拓扑声明
# ---------------------------------------------------------------------------


def test_required_topology_roundtrip(client):
    """创建/编辑/读取：所需拓扑随用例落库并对前端可见。"""
    case = client.post(
        "/text-cases",
        json={"title": "拓扑声明", "required_topology": TOPOLOGY},
    ).json()
    assert case["required_topology"] == TOPOLOGY

    got = client.get(f"/text-cases/{case['id']}").json()
    assert got["required_topology"] == TOPOLOGY

    updated = client.patch(
        f"/text-cases/{case['id']}", json={"required_topology": {"bbu": 2}}
    ).json()
    assert updated["required_topology"] == {"bbu": 2}
    assert client.get(f"/text-cases/{case['id']}").json()["required_topology"] == {"bbu": 2}


# ---------------------------------------------------------------------------
# 三值分支：ready / needs_create / needs_modify（故事 17-21）
# ---------------------------------------------------------------------------


def test_execute_ready_enqueues(client, fake_cli, fake_lass, monkeypatch):
    """LASS ready：任务入队并带 env_check_result=ready，用例 generated→queued。"""
    made = _make_executable_case(client, fake_cli, monkeypatch)
    case_id = made["case"]["id"]
    client.patch(f"/text-cases/{case_id}", json={"required_topology": TOPOLOGY})
    _sandbox_passed(client, made["executable"]["id"])

    fake_lass.respond({"result": "ready"})
    resp = client.post(
        f"/executable-cases/{made['executable']['id']}/execute",
        json={"confirm_inconclusive": False},
    )
    assert resp.status_code == 201, resp.text
    task = resp.json()
    assert task["status"] == "queued"
    assert task["execution_target"] == "real"
    assert task["env_check_result"] == "ready"
    # LASS 收到的拓扑即用例声明（故事 16→17 链路）
    assert fake_lass.requests == [{"required_topology": TOPOLOGY}]
    assert client.get(f"/text-cases/{case_id}").json()["status"] == "queued"


def test_execute_needs_create_blocks_with_missing_list(
    client, fake_cli, fake_lass, monkeypatch
):
    """needs_create：阻断——任务/用例转 done，附缺失资源清单；不占 Worker 队列。"""
    made = _make_executable_case(client, fake_cli, monkeypatch)
    _sandbox_passed(client, made["executable"]["id"])
    fake_lass.respond(
        {
            "result": "needs_create",
            "missing": [
                {"resource": "bbu", "spec": "任意 BBU", "count": 1},
                {"resource": "instrument", "spec": "rf-power-meter", "count": 1},
            ],
        }
    )
    resp = client.post(
        f"/executable-cases/{made['executable']['id']}/execute",
        json={"confirm_inconclusive": False},
    )
    assert resp.status_code == 201, resp.text
    task = resp.json()
    assert task["status"] == "done"  # done(blocked)
    assert task["env_check_result"] == "needs_create"
    assert task["env_check_detail"]["missing"][0]["resource"] == "bbu"
    assert client.get(f"/text-cases/{made['case']['id']}").json()["status"] == "done"

    # 阻断任务不进 Worker 队列（real Worker 领不到）
    _register(client, "real-01", ("real",))
    assert _claim(client, "real-01", ("real",)).status_code == 204


def test_execute_needs_modify_blocks_with_diff(client, fake_cli, fake_lass, monkeypatch):
    """needs_modify：阻断并附差异说明（故事中台维护优化的输入）。"""
    made = _make_executable_case(client, fake_cli, monkeypatch)
    _sandbox_passed(client, made["executable"]["id"])
    fake_lass.respond(
        {
            "result": "needs_modify",
            "diff": "现有环境缺 rf-power-meter 仪表，且 BBU 版本需 ≥ V100R021",
            "changes": [{"add": "instrument:rf-power-meter"}, {"upgrade": "bbu:V100R021"}],
        }
    )
    resp = client.post(
        f"/executable-cases/{made['executable']['id']}/execute",
        json={"confirm_inconclusive": False},
    )
    assert resp.status_code == 201, resp.text
    task = resp.json()
    assert task["status"] == "done"
    assert task["env_check_result"] == "needs_modify"
    assert "rf-power-meter" in task["env_check_detail"]["diff"]
    assert client.get(f"/text-cases/{made['case']['id']}").json()["status"] == "done"


# ---------------------------------------------------------------------------
# 复检闭环（故事 22）
# ---------------------------------------------------------------------------


def _blocked_case(client, fake_cli, fake_lass, monkeypatch) -> dict:
    """造一个 needs_create 阻断的用例，返回 made 结构。"""
    made = _make_executable_case(client, fake_cli, monkeypatch)
    _sandbox_passed(client, made["executable"]["id"])
    fake_lass.respond({"result": "needs_create", "missing": [{"resource": "bbu"}]})
    resp = client.post(
        f"/executable-cases/{made['executable']['id']}/execute",
        json={"confirm_inconclusive": False},
    )
    assert resp.json()["status"] == "done"
    return made


def test_recheck_after_env_ready_enqueues(client, fake_cli, fake_lass, monkeypatch):
    """环境中台处理后复检：ready → 新任务正常入队，用例 done→queued。"""
    made = _blocked_case(client, fake_cli, fake_lass, monkeypatch)
    fake_lass.respond({"result": "ready"})
    resp = client.post(f"/executable-cases/{made['executable']['id']}/recheck")
    assert resp.status_code == 201, resp.text
    task = resp.json()
    assert task["status"] == "queued"
    assert task["env_check_result"] == "ready"
    assert client.get(f"/text-cases/{made['case']['id']}").json()["status"] == "queued"
    # 阻断任务保留为历史（两次校验各一条 real 任务）
    history = client.get(f"/executable-cases/{made['executable']['id']}/executions").json()
    assert [t["env_check_result"] for t in history] == ["ready", "needs_create"]


def test_recheck_still_blocked_stays_done(client, fake_cli, fake_lass, monkeypatch):
    """复检仍未满足：新落一条阻断任务（保留历史），用例保持 done。"""
    made = _blocked_case(client, fake_cli, fake_lass, monkeypatch)
    fake_lass.respond({"result": "needs_modify", "diff": "BBU 版本仍偏低"})
    resp = client.post(f"/executable-cases/{made['executable']['id']}/recheck")
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "done"
    assert resp.json()["env_check_result"] == "needs_modify"
    assert client.get(f"/text-cases/{made['case']['id']}").json()["status"] == "done"


def test_recheck_rejected_when_not_blocked(client, fake_cli, fake_lass, monkeypatch):
    """非阻断状态（如 generated/queued）不允许复检。"""
    made = _make_executable_case(client, fake_cli, monkeypatch)
    resp = client.post(f"/executable-cases/{made['executable']['id']}/recheck")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "not_blocked"


# ---------------------------------------------------------------------------
# 闸门顺序与故障语义（故事 44；不假绿）
# ---------------------------------------------------------------------------


def test_inconclusive_without_confirm_rejected_before_lass(
    client, fake_cli, fake_lass, monkeypatch
):
    """沙盒 inconclusive 未带确认标记：409 拒绝入队，且不消耗 LASS 校验。"""
    made = _make_executable_case(client, fake_cli, monkeypatch)
    _sandbox_verdict(
        client, made["executable"]["id"], "inconclusive",
        [{"seq": 1, "sim_level": "schema_stub", "status": "pass"}],
    )
    resp = client.post(
        f"/executable-cases/{made['executable']['id']}/execute",
        json={"confirm_inconclusive": False},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "confirmation_required"
    assert fake_lass.requests == []  # 软件正确性闸门先于环境校验


def test_sandbox_failed_rejected_before_lass(client, fake_cli, fake_lass, monkeypatch):
    """沙盒 failed：一律拒绝（只许回上游修复），同样不打到 LASS。"""
    made = _make_executable_case(client, fake_cli, monkeypatch)
    _sandbox_verdict(
        client, made["executable"]["id"], "failed",
        [{"seq": 1, "sim_level": "simulated", "status": "fail"}],
    )
    resp = client.post(
        f"/executable-cases/{made['executable']['id']}/execute",
        json={"confirm_inconclusive": True},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "sandbox_failed"
    assert fake_lass.requests == []


def test_lass_unavailable_returns_503(client, fake_cli, monkeypatch):
    """LASS 未配置/不可达：503——校验未发生，不产生任何环境结论（不假绿）。"""
    made = _make_executable_case(client, fake_cli, monkeypatch)
    _sandbox_passed(client, made["executable"]["id"])
    monkeypatch.setattr(settings, "lass_api_url", "", raising=False)
    resp = client.post(
        f"/executable-cases/{made['executable']['id']}/execute",
        json={"confirm_inconclusive": False},
    )
    assert resp.status_code == 503
    # 未产生任务、用例状态未迁移
    assert client.get(f"/text-cases/{made['case']['id']}").json()["status"] == "generated"
    assert client.get(f"/executable-cases/{made['executable']['id']}/executions").json() == []


def test_inconclusive_confirmed_then_lass_checked(client, fake_cli, fake_lass, monkeypatch):
    """inconclusive 带确认标记：过沙盒闸门后正常进入 LASS 校验并阻断。"""
    made = _make_executable_case(client, fake_cli, monkeypatch)
    _sandbox_verdict(
        client, made["executable"]["id"], "inconclusive",
        [{"seq": 1, "sim_level": "schema_stub", "status": "pass"}],
    )
    fake_lass.respond({"result": "needs_create", "missing": [{"resource": "ue"}]})
    resp = client.post(
        f"/executable-cases/{made['executable']['id']}/execute",
        json={"confirm_inconclusive": True},
    )
    assert resp.status_code == 201
    assert resp.json()["env_check_result"] == "needs_create"
    assert len(fake_lass.requests) == 1
