"""execution 模块：Worker 注册/心跳/能力路由领取/结果回传与沙盒调试任务（T8）。

职责（spec 模块划分 execution 的 T8 切片；LASS 校验与真实执行在 T11/T12）：
- Worker 注册表：启动注册（同 worker_id 重复注册视为续约更新）、保活心跳、
  心跳超时摘除
- 任务队列：execution_task 行即队列（DB 唯一主存，ADR-0005）；沙盒调试任务由
  POST /executable-cases/{id}/debug 在 generated 态内创建（不产生用例状态迁移）
- 能力路由：claim 按任务 execution_target + Worker 声明能力过滤，先到先得
  （按入队序），并发领取以条件 UPDATE 兜底恰好一个赢家（故事 29）
- 幂等领取：同一 Worker 重试 claim 返回其已持有的同一任务，不重复派发
  （断网重试不重复执行同一任务，故事 29/32）
- 故障恢复：任务心跳超时（Worker 崩溃/断网）→ 任务回收为 queued 可被重领；
  Worker 心跳超时 → 从注册表摘除（故事 32）

并发说明：sqlite 测试库经 WAL + busy_timeout 串行化写者；生产 PostgreSQL
下条件 UPDATE 的行级谓词同样保证原子领取，无需 SELECT FOR UPDATE。

已知边界（MVP 接受）：任务因心跳超时被回收重领后，原 Worker 若仍在执行，
两个执行者会并发跑同一任务——原 Worker 回传时得 409，结果不被覆盖，但
"重复执行"本身无法在超时窗口内被阻止（fencing token 出 MVP 范围；沙盒
任务幂等无害，real 通路的防护在 T12 评估）。
"""
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ExecutableCase, TextCase, TextCaseStatus
from app.models.execution import ExecutionResult, ExecutionTask, Worker
from app.schemas import TaskResultIn, WorkerRegisterIn

# 并发领取撞车时重选候选的上限（赢者恰好一个由条件 UPDATE 保证）
_CLAIM_MAX_ATTEMPTS = 3


class UnknownWorker(Exception):
    """claim/心跳引用的 worker_id 未注册（或已因超时被摘除）；路由转 404。"""


class TaskNotFound(Exception):
    """任务不存在；路由转 404。"""


class TaskConflict(Exception):
    """任务状态与请求语义冲突（非持有者操作/重复回传等）；路由转 409。"""


class DebugConflict(Exception):
    """沙盒调试发起前提不满足（用例不在 generated 态）；路由转 409。"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Worker 注册与保活
# ---------------------------------------------------------------------------


def register_worker(db: Session, payload: WorkerRegisterIn) -> Worker:
    """注册或续约 Worker；重复注册以最新声明覆盖能力/版本/标签。"""
    now = _utcnow()
    _reap_stale(db, now)  # 顺手摘除/回收：无领取流量时注册也驱动超时治理
    worker = db.get(Worker, payload.worker_id)
    if worker is None:
        worker = Worker(worker_id=payload.worker_id)
        db.add(worker)
    worker.capabilities = list(payload.capabilities)
    worker.sim_package_version = payload.sim_package_version
    worker.topology_tags = payload.topology_tags
    worker.last_heartbeat = now
    db.commit()
    db.refresh(worker)
    return worker


def heartbeat_worker(db: Session, worker_id: str) -> Worker:
    """Worker 保活心跳：续约 last_heartbeat；未注册/已摘除抛 UnknownWorker。"""
    now = _utcnow()
    _reap_stale(db, now)
    worker = db.get(Worker, worker_id)
    if worker is None:
        raise UnknownWorker(worker_id)
    worker.last_heartbeat = now
    db.commit()
    db.refresh(worker)
    return worker


def _reap_stale(db: Session, now: datetime) -> None:
    """心跳超时摘除：摘除失活 Worker；回收其（及任何）心跳超时的在领任务。

    回收即回 queued 并清空领取痕迹，任务可被任意能力匹配的 Worker 重领。
    """
    cutoff = now - timedelta(seconds=settings.worker_heartbeat_timeout_seconds)
    stale_workers = db.execute(
        select(Worker).where(Worker.last_heartbeat < cutoff)
    ).scalars().all()
    for worker in stale_workers:
        db.delete(worker)
    stale_tasks = db.execute(
        select(ExecutionTask).where(
            ExecutionTask.status == "claimed",
            ExecutionTask.heartbeat_at < cutoff,
        )
    ).scalars().all()
    for task in stale_tasks:
        task.status = "queued"
        task.worker_id = None
        task.claimed_at = None
        task.heartbeat_at = None
    db.flush()


# ---------------------------------------------------------------------------
# 沙盒调试任务
# ---------------------------------------------------------------------------


def create_debug_task(db: Session, exec_case: ExecutableCase) -> ExecutionTask:
    """为可执行用例发起一次沙盒调试任务（spec API 契约）。

    沙盒调试在 generated 态内闭环（ADR-0009）：只创建 execution_target=sandbox
    的 queued 任务，不迁移用例状态；调试会话业务记录（debug_run）在 T9/T10。
    """
    case = db.get(TextCase, exec_case.text_case_id)
    if case is None or case.status != TextCaseStatus.GENERATED:
        raise DebugConflict("仅 generated 态用例可发起沙盒调试")
    task = ExecutionTask(
        executable_case_id=exec_case.id,
        execution_target="sandbox",
        status="queued",
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


# ---------------------------------------------------------------------------
# 能力路由领取
# ---------------------------------------------------------------------------


def claim_task(
    db: Session, worker_id: str, capabilities: Sequence[str]
) -> Optional[ExecutionTask]:
    """按注册能力过滤领取任务：先到先得、幂等、并发安全；无可领任务返回 None。

    - 领取前先摘除超时 Worker 并回收其在领任务（崩溃任务自动回到队列）
    - 同一 Worker 已有在领任务时直接返回之（断网重试语义无副作用）
    - 候选选取后条件 UPDATE（status='queued'）兜底并发：rowcount==0 即被抢先，回滚重选
    - **服务端以注册表能力为准**：任务过滤只用 ``worker.capabilities``；claim
      请求自报的 capabilities（可空）若超出注册范围即 409——自报不扩大权限，
      防止恶意/过时的客户端绕过双向隔离（ADR-0009）
    """
    worker = db.get(Worker, worker_id)
    if worker is None:
        raise UnknownWorker(worker_id)
    registered_caps = set(worker.capabilities)
    claimed_caps = set(capabilities)
    if not claimed_caps.issubset(registered_caps):
        raise TaskConflict(
            f"claim capabilities {sorted(claimed_caps)} exceed registered {sorted(registered_caps)}"
        )

    for _attempt in range(_CLAIM_MAX_ATTEMPTS):
        now = _utcnow()
        _reap_stale(db, now)

        # 幂等：重试 claim 拿回同一在领任务，不领新任务
        existing = db.execute(
            select(ExecutionTask).where(
                ExecutionTask.status == "claimed",
                ExecutionTask.worker_id == worker_id,
            )
        ).scalars().first()
        if existing is not None:
            existing.heartbeat_at = now
            worker.last_heartbeat = now
            db.commit()
            db.refresh(existing)
            return existing

        candidate = db.execute(
            select(ExecutionTask)
            .where(
                ExecutionTask.status == "queued",
                ExecutionTask.execution_target.in_(sorted(registered_caps)),
            )
            .order_by(ExecutionTask.id)
            .limit(1)
        ).scalars().first()
        if candidate is None:
            db.commit()  # 提交本轮摘除/回收
            return None

        result = db.execute(
            update(ExecutionTask)
            .where(ExecutionTask.id == candidate.id, ExecutionTask.status == "queued")
            .values(
                status="claimed",
                worker_id=worker_id,
                claimed_at=now,
                heartbeat_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            db.rollback()  # 被并发 Worker 抢先，重选候选
            continue
        worker.last_heartbeat = now
        db.commit()
        db.refresh(candidate)
        return candidate

    return None  # 连续撞车（理论上限），Worker 下轮轮询再来


# ---------------------------------------------------------------------------
# 任务心跳与结果回传
# ---------------------------------------------------------------------------


def heartbeat_task(db: Session, task_id: int, worker_id: str) -> ExecutionTask:
    """任务执行中心跳：续约任务心跳（防回收）并顺带续约 Worker 保活。

    持有者必须仍在注册表内——被摘除的 Worker 不能再续约任务（摘除即失效）。
    """
    task = db.get(ExecutionTask, task_id)
    if task is None:
        raise TaskNotFound(task_id)
    if task.status != "claimed" or task.worker_id != worker_id:
        raise TaskConflict(f"task {task_id} not claimed by {worker_id}")
    worker = db.get(Worker, worker_id)
    if worker is None:
        raise UnknownWorker(worker_id)
    now = _utcnow()
    task.heartbeat_at = now
    worker.last_heartbeat = now
    db.commit()
    db.refresh(task)
    return task


def submit_result(
    db: Session, task_id: int, payload: TaskResultIn
) -> ExecutionResult:
    """结果回传：仅持有者可回传；任务转 done 并落 execution_result。

    重复回传（Worker 未收到响应的重试）抛 TaskConflict——已有结果不被覆盖，
    Worker 侧将 409 视为"已记录"（故事 29 幂等）。
    沙盒通路不迁移用例状态（generated 态内闭环）；real 通路状态机在 T12。
    sim_package_version 取执行 Worker 的注册声明（ADR-0009：沙盒报告记录仿真
    包版本，为仿真与 catalog 漂移排查留证据），不信任 Worker 回传自报。
    """
    task = db.get(ExecutionTask, task_id)
    if task is None:
        raise TaskNotFound(task_id)
    if task.status != "claimed" or task.worker_id != payload.worker_id:
        raise TaskConflict(f"task {task_id} not claimed by {payload.worker_id}")
    worker = db.get(Worker, payload.worker_id)
    if worker is None:
        raise UnknownWorker(payload.worker_id)
    now = _utcnow()
    task.status = "done"
    task.finished_at = now
    result = ExecutionResult(
        task_id=task.id,
        verdict=payload.verdict,
        logs=payload.logs,
        step_results=payload.step_results,
        artifacts=payload.artifacts,
        sim_package_version=worker.sim_package_version,
    )
    db.add(result)
    db.commit()
    db.refresh(result)
    return result
