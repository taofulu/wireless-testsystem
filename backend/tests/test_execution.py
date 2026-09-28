"""系统级测试：沙盒 Worker 注册、能力路由与沙盒执行通路（T8，故事 28–32、45）。

守护的验收标准（Issue #9）：
- POST /worker/register 接收 worker_id/capabilities/sim_package_version/
  topology_tags；心跳续约，超时摘除
- claim 按 execution_target+能力过滤、先到先得且幂等；sandbox 任务不被
  real Worker 领取（双向隔离断言）
- 心跳超时/崩溃后任务可被重新领取
- 沙盒调试任务从 generated 态用例发起（不产生用例状态迁移，ADR-0009）

Worker 侧"拉代码→临时落盘→真实 pytest 子进程→回传"由 worker 包的真实
进程端到端测试守护（worker/tests/test_run_e2e.py）。
"""
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db import SessionLocal
from app.models.execution import ExecutionTask, Worker

CATALOG_DATA = Path(__file__).resolve().parents[1] / "app" / "catalog_data"


# ---------------------------------------------------------------------------
# 夹具与助手
# ---------------------------------------------------------------------------
# fake_cli 夹具统一由 conftest 提供（系统边界 fake，ADR-0006）


def _import_demo_dictionary(client) -> None:
    payload = json.loads((CATALOG_DATA / "command_dictionary.json").read_text(encoding="utf-8"))
    resp = client.post("/command-dictionaries/import", json=payload)
    assert resp.status_code == 201, resp.text


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


def _make_executable_case(client, fake_cli, monkeypatch) -> dict:
    """全链路产出 generated 态用例 + 一个可执行用例版本。"""
    _import_demo_dictionary(client)
    monkeypatch.setenv("FAKE_GLM_MAPPING", "matched")
    case = client.post(
        "/text-cases",
        json={
            "title": "切换频段验证",
            "precondition": "基站已加电，BBU 与小区状态正常",
            "steps_text": "1. 激活目标小区\n2. 查询小区状态",
            "expected_text": "1. 小区状态为激活\n2. 返回小区状态",
        },
    ).json()
    assert client.post(f"/text-cases/{case['id']}/elaboration/skip").status_code == 200
    assert client.post(f"/text-cases/{case['id']}/map").status_code == 202
    job = _poll_mapping(client, case["id"])
    assert job["unmapped_count"] == 0, job
    assert client.post(f"/text-cases/{case['id']}/confirm").status_code == 200
    resp = client.post(f"/text-cases/{case['id']}/generate")
    assert resp.status_code == 201, resp.text
    return {"case": case, "executable": resp.json()}


def _register(client, worker_id: str, capabilities=("sandbox",), **extra) -> dict:
    resp = client.post(
        "/worker/register",
        json={"worker_id": worker_id, "capabilities": list(capabilities), **extra},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _claim(client, worker_id: str, capabilities=("sandbox",)):
    return client.post(
        "/worker/tasks/claim",
        json={"worker_id": worker_id, "capabilities": list(capabilities)},
    )


def _debug_task(client, exec_id: int) -> dict:
    resp = client.post(f"/executable-cases/{exec_id}/debug")
    assert resp.status_code == 201, resp.text
    return resp.json()


def _seed_task(executable_case_id: int, execution_target: str = "sandbox", **fields) -> int:
    """直接落库一条执行任务（real 任务的创建入口在 T12，这里越层播种）。"""
    db = SessionLocal()
    try:
        task = ExecutionTask(
            executable_case_id=executable_case_id,
            execution_target=execution_target,
            **fields,
        )
        db.add(task)
        db.commit()
        return task.id
    finally:
        db.close()


def _get_task(task_id: int) -> ExecutionTask:
    db = SessionLocal()
    try:
        return db.get(ExecutionTask, task_id)
    finally:
        db.close()


def _get_worker(worker_id: str):
    db = SessionLocal()
    try:
        return db.get(Worker, worker_id)
    finally:
        db.close()


def _ago(**kwargs) -> datetime:
    return datetime.now(timezone.utc) - timedelta(**kwargs)


# ---------------------------------------------------------------------------
# Worker 注册与心跳（验收：register 接收四字段；心跳续约，超时摘除）
# ---------------------------------------------------------------------------


def test_register_worker_persists_declared_fields(client):
    body = _register(
        client,
        "sandbox-01",
        ("sandbox",),
        sim_package_version="0.3.1",
        topology_tags=["lab-a"],
    )
    assert body["worker_id"] == "sandbox-01"
    worker = _get_worker("sandbox-01")
    assert worker is not None
    assert worker.capabilities == ["sandbox"]
    assert worker.sim_package_version == "0.3.1"
    assert worker.topology_tags == ["lab-a"]
    assert worker.last_heartbeat is not None


def test_register_same_worker_id_renews_registration(client):
    _register(client, "sandbox-01", ("sandbox",), sim_package_version="0.3.1")
    body = _register(client, "sandbox-01", ("sandbox", "real"), sim_package_version="0.4.0")
    assert body["worker_id"] == "sandbox-01"
    worker = _get_worker("sandbox-01")
    assert worker.capabilities == ["sandbox", "real"]
    assert worker.sim_package_version == "0.4.0"


def test_register_rejects_empty_capabilities(client):
    resp = client.post("/worker/register", json={"worker_id": "w1", "capabilities": []})
    assert resp.status_code == 422


def test_register_rejects_unknown_capability(client):
    resp = client.post(
        "/worker/register", json={"worker_id": "w1", "capabilities": ["quantum"]}
    )
    assert resp.status_code == 422


def test_heartbeat_renews_and_unknown_worker_404(client):
    _register(client, "sandbox-01")
    resp = client.post("/worker/heartbeat", json={"worker_id": "sandbox-01"})
    assert resp.status_code == 200
    resp = client.post("/worker/heartbeat", json={"worker_id": "ghost"})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 沙盒调试任务发起（generated 态内闭环，不产生用例状态迁移）
# ---------------------------------------------------------------------------


def test_debug_creates_queued_sandbox_task(client, fake_cli, monkeypatch):
    made = _make_executable_case(client, fake_cli, monkeypatch)
    task = _debug_task(client, made["executable"]["id"])
    assert task["execution_target"] == "sandbox"
    assert task["status"] == "queued"
    assert task["worker_id"] is None
    case = client.get(f"/text-cases/{made['case']['id']}").json()
    assert case["status"] == "generated"  # 沙盒调试不产生状态迁移（ADR-0009）


def test_debug_404_unknown_executable_case(client):
    resp = client.post("/executable-cases/9999/debug")
    assert resp.status_code == 404


def test_debug_requires_existing_executable_version(client, fake_cli, monkeypatch):
    """confirmed 但从未生成的用例没有任何可执行版本：任何 exec id 都 404。

    （generated → mapped 的上游重开在 T10；T8 里存在 exec 版本的用例必在
    generated 态，409 分支暂不可达，不预埋。）
    """
    _import_demo_dictionary(client)
    monkeypatch.setenv("FAKE_GLM_MAPPING", "matched")
    case = client.post(
        "/text-cases",
        json={"title": "未生成", "precondition": "p", "steps_text": "s", "expected_text": "e"},
    ).json()
    assert client.post(f"/text-cases/{case['id']}/elaboration/skip").status_code == 200
    assert client.post(f"/text-cases/{case['id']}/map").status_code == 202
    _poll_mapping(client, case["id"])
    assert client.post(f"/text-cases/{case['id']}/confirm").status_code == 200
    versions = client.get(f"/text-cases/{case['id']}/executable-cases").json()
    assert versions == []
    assert client.post("/executable-cases/1/debug").status_code == 404


# ---------------------------------------------------------------------------
# 能力路由与领取（验收：能力过滤、先到先得、幂等、双向隔离）
# ---------------------------------------------------------------------------


def test_claim_sandbox_task_not_taken_by_real_worker(client, fake_cli, monkeypatch):
    made = _make_executable_case(client, fake_cli, monkeypatch)
    task = _debug_task(client, made["executable"]["id"])
    _register(client, "real-01", ("real",))
    resp = _claim(client, "real-01", ("real",))
    assert resp.status_code == 204  # sandbox 任务对 real Worker 不可见

    _register(client, "sandbox-01", ("sandbox",))
    resp = _claim(client, "sandbox-01", ("sandbox",))
    assert resp.status_code == 200
    assert resp.json()["task_id"] == task["id"]


def test_claim_real_task_not_taken_by_sandbox_worker(client, fake_cli, monkeypatch):
    made = _make_executable_case(client, fake_cli, monkeypatch)
    real_task_id = _seed_task(made["executable"]["id"], execution_target="real")
    _register(client, "sandbox-01", ("sandbox",))
    resp = _claim(client, "sandbox-01", ("sandbox",))
    assert resp.status_code == 204  # real 任务对 sandbox Worker 不可见

    _register(client, "real-01", ("real",))
    resp = _claim(client, "real-01", ("real",))
    assert resp.status_code == 200
    assert resp.json()["task_id"] == real_task_id


def test_claim_first_come_first_served_fifo(client, fake_cli, monkeypatch):
    made = _make_executable_case(client, fake_cli, monkeypatch)
    first = _debug_task(client, made["executable"]["id"])
    second = _debug_task(client, made["executable"]["id"])
    _register(client, "sandbox-01")
    _register(client, "sandbox-02")
    resp1 = _claim(client, "sandbox-01")
    resp2 = _claim(client, "sandbox-02")
    assert {resp1.json()["task_id"], resp2.json()["task_id"]} == {first["id"], second["id"]}
    assert resp1.json()["task_id"] == first["id"]  # 先到先得按入队序


def test_claim_is_idempotent_for_same_worker(client, fake_cli, monkeypatch):
    """断网重试不重复领取：同一 Worker 重试 claim 拿回同一任务（故事 29/32）。"""
    made = _make_executable_case(client, fake_cli, monkeypatch)
    first = _debug_task(client, made["executable"]["id"])
    _debug_task(client, made["executable"]["id"])
    _register(client, "sandbox-01")
    resp1 = _claim(client, "sandbox-01")
    resp2 = _claim(client, "sandbox-01")  # 重试：仍持有 first，不领新任务
    assert resp1.json()["task_id"] == first["id"]
    assert resp2.json()["task_id"] == first["id"]


def test_claim_204_when_queue_empty(client):
    _register(client, "sandbox-01")
    resp = _claim(client, "sandbox-01")
    assert resp.status_code == 204


def test_claim_requires_registered_worker(client):
    resp = _claim(client, "ghost")
    assert resp.status_code == 404


def test_claim_capability_self_report_cannot_exceed_registration(client, fake_cli, monkeypatch):
    """服务端以注册表能力为准：自报超出注册能力的 claim 被 409 拒绝（双向隔离不被绕过）。"""
    made = _make_executable_case(client, fake_cli, monkeypatch)
    real_task_id = _seed_task(made["executable"]["id"], execution_target="real")
    _register(client, "sandbox-01", ("sandbox",))
    resp = _claim(client, "sandbox-01", ("sandbox", "real"))  # 自报越权
    assert resp.status_code == 409
    # 正常自报（子集）也领不到 real 任务——过滤以注册能力为准
    assert _get_task(real_task_id).status == "queued"


def test_result_records_registered_sim_package_version(client, fake_cli, monkeypatch):
    """沙盒报告记录仿真包版本（ADR-0009）：取执行 Worker 的注册声明。"""
    made = _make_executable_case(client, fake_cli, monkeypatch)
    task = _debug_task(client, made["executable"]["id"])
    _register(client, "sandbox-01", ("sandbox",), sim_package_version="sim-1.2.3")
    _claim(client, "sandbox-01")
    resp = client.post(
        f"/worker/tasks/{task['id']}/result",
        json={"worker_id": "sandbox-01", "verdict": "passed", "logs": "ok", "step_results": []},
    )
    assert resp.status_code == 201
    assert _get_task(task["id"]).result.sim_package_version == "sim-1.2.3"


def test_task_heartbeat_rejected_after_worker_deregistered(client, fake_cli, monkeypatch):
    """摘除即失效：Worker 被超时摘除后，其在领任务的心跳也被拒绝（404）。"""
    made = _make_executable_case(client, fake_cli, monkeypatch)
    task = _debug_task(client, made["executable"]["id"])
    _register(client, "sandbox-01")
    _claim(client, "sandbox-01")

    # Worker 失活（注册心跳陈旧），任务心跳仍新鲜（如摘除前最后一跳）
    db = SessionLocal()
    try:
        db.get(Worker, "sandbox-01").last_heartbeat = _ago(seconds=3600)
        db.commit()
    finally:
        db.close()

    _register(client, "sandbox-02")  # 任意流量驱动摘除（不限于 claim）
    assert _get_worker("sandbox-01") is None
    resp = client.post(
        f"/worker/tasks/{task['id']}/heartbeat", json={"worker_id": "sandbox-01"}
    )
    assert resp.status_code == 404


def test_claim_returns_executable_case_ref(client, fake_cli, monkeypatch):
    made = _make_executable_case(client, fake_cli, monkeypatch)
    _debug_task(client, made["executable"]["id"])
    _register(client, "sandbox-01")
    body = _claim(client, "sandbox-01").json()
    assert body["executable_case_id"] == made["executable"]["id"]
    assert body["execution_target"] == "sandbox"
    task = _get_task(body["task_id"])
    assert task.status == "claimed"
    assert task.worker_id == "sandbox-01"
    assert task.claimed_at is not None
    assert task.heartbeat_at is not None


def test_claim_concurrent_workers_only_one_wins(client, fake_cli, monkeypatch):
    """两个 Worker 并发抢同一任务：恰好一个成功（幂等，不重复执行）。"""
    from app.execution import claim_task

    made = _make_executable_case(client, fake_cli, monkeypatch)
    task = _debug_task(client, made["executable"]["id"])
    _register(client, "sandbox-01")
    _register(client, "sandbox-02")

    winners: list[str] = []
    barrier = threading.Barrier(2)

    def _race(worker_id: str) -> None:
        db = SessionLocal()
        try:
            barrier.wait(timeout=5)
            claimed = claim_task(db, worker_id, ["sandbox"])
            if claimed is not None:
                winners.append(worker_id)
        finally:
            db.close()

    threads = [
        threading.Thread(target=_race, args=("sandbox-01",)),
        threading.Thread(target=_race, args=("sandbox-02",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert len(winners) == 1
    assert _get_task(task["id"]).worker_id == winners[0]


# ---------------------------------------------------------------------------
# 心跳超时与重新领取（验收：超时摘除、崩溃后任务可重领）
# ---------------------------------------------------------------------------


def test_stale_claimed_task_reclaimable_by_other_worker(client, fake_cli, monkeypatch):
    made = _make_executable_case(client, fake_cli, monkeypatch)
    task = _debug_task(client, made["executable"]["id"])
    _register(client, "sandbox-01")
    assert _claim(client, "sandbox-01").json()["task_id"] == task["id"]

    # 模拟 Worker 崩溃：任务心跳停在超时之前
    db = SessionLocal()
    try:
        row = db.get(ExecutionTask, task["id"])
        row.heartbeat_at = _ago(seconds=3600)
        db.commit()
    finally:
        db.close()

    _register(client, "sandbox-02")
    resp = _claim(client, "sandbox-02")
    assert resp.status_code == 200
    assert resp.json()["task_id"] == task["id"]
    assert _get_task(task["id"]).worker_id == "sandbox-02"


def test_fresh_claimed_task_not_reclaimed(client, fake_cli, monkeypatch):
    made = _make_executable_case(client, fake_cli, monkeypatch)
    task = _debug_task(client, made["executable"]["id"])
    _register(client, "sandbox-01")
    _register(client, "sandbox-02")
    assert _claim(client, "sandbox-01").json()["task_id"] == task["id"]
    # 心跳新鲜：其他 Worker 领不到
    assert _claim(client, "sandbox-02").status_code == 204


def test_stale_worker_deregistered_on_reap(client, fake_cli, monkeypatch):
    made = _make_executable_case(client, fake_cli, monkeypatch)
    task = _debug_task(client, made["executable"]["id"])
    _register(client, "sandbox-01")
    _claim(client, "sandbox-01")

    db = SessionLocal()
    try:
        worker = db.get(Worker, "sandbox-01")
        worker.last_heartbeat = _ago(seconds=3600)
        row = db.get(ExecutionTask, task["id"])
        row.heartbeat_at = _ago(seconds=3600)
        db.commit()
    finally:
        db.close()

    _register(client, "sandbox-02")
    assert _claim(client, "sandbox-02").json()["task_id"] == task["id"]
    assert _get_worker("sandbox-01") is None  # 心跳超时摘除
    # 被摘除的 Worker 再 claim 视为未注册
    assert _claim(client, "sandbox-01").status_code == 404


def test_task_heartbeat_renews_and_guards(client, fake_cli, monkeypatch):
    made = _make_executable_case(client, fake_cli, monkeypatch)
    task = _debug_task(client, made["executable"]["id"])
    _register(client, "sandbox-01")
    _register(client, "sandbox-02")
    _claim(client, "sandbox-01")

    resp = client.post(
        f"/worker/tasks/{task['id']}/heartbeat", json={"worker_id": "sandbox-01"}
    )
    assert resp.status_code == 200

    # 非持有者心跳 → 409；未知任务 → 404
    resp = client.post(
        f"/worker/tasks/{task['id']}/heartbeat", json={"worker_id": "sandbox-02"}
    )
    assert resp.status_code == 409
    resp = client.post("/worker/tasks/9999/heartbeat", json={"worker_id": "sandbox-01"})
    assert resp.status_code == 404

    # 心跳续约后任务不被重领
    assert _claim(client, "sandbox-02").status_code == 204


# ---------------------------------------------------------------------------
# 结果回传（验收：执行后回传结果落库；重复回传不重复执行语义）
# ---------------------------------------------------------------------------


def _claim_and_get_task(client, made) -> dict:
    task = _debug_task(client, made["executable"]["id"])
    _register(client, "sandbox-01")
    _claim(client, "sandbox-01")
    return task


def test_result_submission_marks_task_done(client, fake_cli, monkeypatch):
    made = _make_executable_case(client, fake_cli, monkeypatch)
    task = _claim_and_get_task(client, made)
    resp = client.post(
        f"/worker/tasks/{task['id']}/result",
        json={
            "worker_id": "sandbox-01",
            "verdict": "passed",
            "logs": "1 passed in 0.5s",
            "step_results": [],
            "artifacts": [],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["task_id"] == task["id"]
    assert body["verdict"] == "passed"

    row = _get_task(task["id"])
    assert row.status == "done"
    assert row.finished_at is not None
    assert row.result is not None
    assert row.result.verdict == "passed"
    assert row.result.logs == "1 passed in 0.5s"
    # 沙盒通路不产生用例状态迁移（generated 态内闭环）
    case = client.get(f"/text-cases/{made['case']['id']}").json()
    assert case["status"] == "generated"


def test_result_rejected_when_not_claimed_by_worker(client, fake_cli, monkeypatch):
    made = _make_executable_case(client, fake_cli, monkeypatch)
    task = _debug_task(client, made["executable"]["id"])  # 未领取
    _register(client, "sandbox-01")
    resp = client.post(
        f"/worker/tasks/{task['id']}/result",
        json={"worker_id": "sandbox-01", "verdict": "passed", "logs": "", "step_results": []},
    )
    assert resp.status_code == 409

    # 已被 sandbox-01 领取：其他 Worker 回传同样被拒
    _claim(client, "sandbox-01")
    resp = client.post(
        f"/worker/tasks/{task['id']}/result",
        json={"worker_id": "sandbox-02", "verdict": "passed", "logs": "", "step_results": []},
    )
    assert resp.status_code == 409


def test_result_retry_after_done_is_safe(client, fake_cli, monkeypatch):
    """结果回传响应丢失后 Worker 重试：已 done 的任务返回 409，Worker 视为已记录。"""
    made = _make_executable_case(client, fake_cli, monkeypatch)
    task = _claim_and_get_task(client, made)
    payload = {
        "worker_id": "sandbox-01",
        "verdict": "failed",
        "logs": "assertion failed",
        "step_results": [],
    }
    assert client.post(f"/worker/tasks/{task['id']}/result", json=payload).status_code == 201
    resp = client.post(f"/worker/tasks/{task['id']}/result", json=payload)
    assert resp.status_code == 409
    assert _get_task(task["id"]).result.verdict == "failed"  # 未被重试覆盖


def test_result_404_unknown_task(client):
    _register(client, "sandbox-01")
    resp = client.post(
        "/worker/tasks/9999/result",
        json={"worker_id": "sandbox-01", "verdict": "passed", "logs": "", "step_results": []},
    )
    assert resp.status_code == 404


def test_result_rejects_bad_verdict(client, fake_cli, monkeypatch):
    made = _make_executable_case(client, fake_cli, monkeypatch)
    task = _claim_and_get_task(client, made)
    resp = client.post(
        f"/worker/tasks/{task['id']}/result",
        json={"worker_id": "sandbox-01", "verdict": "green", "logs": "", "step_results": []},
    )
    assert resp.status_code == 422
