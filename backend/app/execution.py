"""execution 模块：Worker 注册/心跳/能力路由领取/结果回传与沙盒调试任务（T8）；
T9/T10 加入调试会话落库与真实执行二次确认闸门；T11 插入 LASS 三值环境校验
闸门（execute/recheck）；T12 接通 real 通路用例状态迁移（queued→running→done）
与 Allure 结果落库。

职责（spec 模块划分 execution）：
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
- 沙盒回传落 debug_run（T9）：sandbox 任务完成即生成调试会话业务记录，
  三态判决与逐步骤仿真标注随会话保留最近 N 次（ADR-0009，不进五环追溯）
- 真实执行闸门（T10）：依据该代码版本最近一次调试会话判决放行——passed 直接
  入队；inconclusive/无调试结论须带二次确认标记（响应附未覆盖步骤清单）；
  failed 一律拒绝，只许回上游修复（故事 43/44）
- LASS 三值环境校验（T11，故事 17-22）：沙盒闸门通过后调用 LASS 校验所需
  拓扑——ready 入队；needs_create/needs_modify 阻断（任务转 done 并带
  env_check_result/env_check_detail，用例转 done）；环境中台处理后经
  recheck 复检闭环。LASS 不可达不产生任何环境结论（不假绿）
- real 通路状态迁移（T12）：real 任务被领取 → 用例 queued→running；结果
  回传 → running→done；Allure 结果原文随 execution_result 落库

并发说明：sqlite 测试库经 WAL + busy_timeout 串行化写者；生产 PostgreSQL
下条件 UPDATE 的行级谓词同样保证原子领取，无需 SELECT FOR UPDATE。

已知边界（MVP 接受）：任务因心跳超时被回收重领后，原 Worker 若仍在执行，
两个执行者会并发跑同一任务——原 Worker 回传时得 409，结果不被覆盖，但
"重复执行"本身无法在超时窗口内被阻止（fencing token 出 MVP 范围；沙盒
任务幂等无害；real 通路同一用例同一时刻最多一个在跑任务由状态机保证：
execute/recheck 放行后用例即离开 generated/done 态，重复提交被 409 拦截）。
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import lass
from app.config import settings
from app.models import ExecutableCase, TextCase, TextCaseStatus
from app.models.execution import ExecutionResult, ExecutionTask, Worker
from app.models.mapping import StructuredStep
from app.sandbox import latest_debug_run, record_debug_run, uncovered_steps
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


def create_debug_task(
    db: Session, exec_case: ExecutableCase, preset: Optional[dict] = None
) -> ExecutionTask:
    """为可执行用例发起一次沙盒调试任务（spec API 契约）。

    沙盒调试在 generated 态内闭环（ADR-0009）：只创建 execution_target=sandbox
    的 queued 任务，不迁移用例状态；调试预设随任务携带，回传后进入
    debug_run 快照（仅存于调试会话上下文，不写入用例正式数据，故事 40）。
    """
    case = db.get(TextCase, exec_case.text_case_id)
    if case is None or case.status != TextCaseStatus.GENERATED:
        raise DebugConflict("仅 generated 态用例可发起沙盒调试")
    task = ExecutionTask(
        executable_case_id=exec_case.id,
        execution_target="sandbox",
        status="queued",
        preset=preset,
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
        if candidate.execution_target == "real":
            # T12：real 任务被领取即用例 queued → running（沙盒调试在 generated
            # 态内闭环，不迁移用例状态）
            _transition_case(db, candidate, {TextCaseStatus.QUEUED}, TextCaseStatus.RUNNING)
        worker.last_heartbeat = now
        db.commit()
        db.refresh(candidate)
        return candidate

    return None  # 连续撞车（理论上限），Worker 下轮轮询再来


# ---------------------------------------------------------------------------
# 用例状态迁移助手（real 通路，T12）
# ---------------------------------------------------------------------------


def _transition_case(
    db: Session,
    task: ExecutionTask,
    allowed_from: set[TextCaseStatus],
    to: TextCaseStatus,
) -> None:
    """real 任务驱动的用例状态迁移；当前状态不在 allowed_from 时不动（防御）。

    沙盒任务永不调用本函数（调试在 generated 态内闭环，ADR-0009）。
    """
    exec_case = db.get(ExecutableCase, task.executable_case_id)
    if exec_case is None:
        return
    case = db.get(TextCase, exec_case.text_case_id)
    if case is not None and case.status in allowed_from:
        case.status = to


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
    沙盒通路不迁移用例状态（generated 态内闭环），回传同时落 debug_run
    调试会话（T9：三态判决 + 逐步骤仿真标注 + preset 快照，保留最近 N 次）；
    real 通路（T12）回传即用例 running → done，Allure 结果原文随
    execution_result 落库（步骤级解析在 T13）。
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
        allure_report=payload.allure_report,
        sim_package_version=worker.sim_package_version,
    )
    db.add(result)
    if task.execution_target == "real":
        _transition_case(
            db, task, {TextCaseStatus.RUNNING, TextCaseStatus.QUEUED}, TextCaseStatus.DONE
        )
    db.commit()
    db.refresh(result)

    if task.execution_target == "sandbox":
        record_debug_run(
            db,
            task,
            verdict=payload.verdict,
            step_results=payload.step_results,
            sim_package_version=worker.sim_package_version,
        )
    return result


# ---------------------------------------------------------------------------
# 真实执行提交（T10 沙盒判决闸门 + T11 LASS 三值环境校验闸门）
# ---------------------------------------------------------------------------


class ExecuteConflict(Exception):
    """真实执行提交被闸门拦截；路由按 code 转 409（附未覆盖步骤清单）。"""

    def __init__(
        self,
        code: str,
        detail: str,
        uncovered: Optional[list[int]] = None,
    ):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.uncovered = uncovered


def _all_step_seqs(db: Session, exec_case: ExecutableCase) -> list[int]:
    """用例全部结构化步骤序号（无调试结论时视为全部未覆盖）。"""
    return list(
        db.execute(
            select(StructuredStep.seq)
            .where(StructuredStep.text_case_id == exec_case.text_case_id)
            .order_by(StructuredStep.seq)
        ).scalars().all()
    )


def _check_sandbox_gate(db: Session, exec_case: ExecutableCase, confirm_inconclusive: bool) -> None:
    """T10 沙盒判决闸门：passed 放行；failed 拒绝；inconclusive 须二次确认。"""
    case = db.get(TextCase, exec_case.text_case_id)
    if case is None or case.status != TextCaseStatus.GENERATED:
        raise ExecuteConflict(
            "not_generated", "仅 generated 态用例可提交真实执行"
        )

    latest = latest_debug_run(db, exec_case.id)
    if latest is not None and latest.verdict == "failed":
        raise ExecuteConflict(
            "sandbox_failed",
            "最近一次沙盒调试判决为 failed：请回确认态改映射或改文本重新映射，"
            "重新生成新版本后再调试",
        )

    if latest is None or latest.verdict != "passed":
        uncovered = (
            uncovered_steps(latest.step_results)
            if latest is not None
            else _all_step_seqs(db, exec_case)
        )
        if not confirm_inconclusive:
            reason = (
                "沙盒判决不可判定" if latest is not None else "尚无沙盒调试结论"
            )
            raise ExecuteConflict(
                "confirmation_required",
                f"{reason}：存在未被仿真覆盖的步骤，提交真实执行需二次确认",
                uncovered=uncovered,
            )


def _create_real_task(
    db: Session,
    exec_case: ExecutableCase,
    case: TextCase,
    outcome: lass.EnvCheckOutcome,
) -> ExecutionTask:
    """按 LASS 三值结论落地 real 任务与用例状态（故事 17-21）。

    - ready：任务 queued 等待 real Worker 领取，用例 generated/done → queued
    - needs_create/needs_modify：阻断——任务直接 done 并携带
      env_check_result/env_check_detail（缺失清单/差异说明），用例 → done；
      不占用 Worker 队列（环境中台处理后经 recheck 闭环，故事 22）
    """
    now = _utcnow()
    ready = outcome.result == "ready"
    task = ExecutionTask(
        executable_case_id=exec_case.id,
        execution_target="real",
        status="queued" if ready else "done",
        env_check_result=outcome.result,
        env_check_detail=outcome.detail,
        finished_at=None if ready else now,
    )
    db.add(task)
    case.status = TextCaseStatus.QUEUED if ready else TextCaseStatus.DONE
    db.commit()
    db.refresh(task)
    return task


def submit_real_execution(
    db: Session, exec_case: ExecutableCase, confirm_inconclusive: bool
) -> ExecutionTask:
    """提交真实执行（POST /executable-cases/{id}/execute，故事 17-21、43/44）。

    闸门顺序（先软件正确性、后环境可用性）：
    1. T10 沙盒判决闸门（见 _check_sandbox_gate）
    2. T11 LASS 三值环境校验：以用例声明的所需拓扑（required_topology）调
       LASS；ready 入队，needs_create/needs_modify 阻断转 done 并附差异
    LASS 未配置/不可达抛 lass.LassUnavailable（路由转 503）——校验未发生
    不产生任何环境结论（不假绿）。
    """
    _check_sandbox_gate(db, exec_case, confirm_inconclusive)
    case = db.get(TextCase, exec_case.text_case_id)
    assert case is not None  # 闸门已校验存在性
    outcome = lass.check_topology(case.required_topology)
    return _create_real_task(db, exec_case, case, outcome)


def recheck_env(db: Session, exec_case: ExecutableCase) -> ExecutionTask:
    """环境复检（POST /executable-cases/{id}/env-recheck，故事 22）。

    环境中台完成新建/修改后再次触发 LASS 校验：ready 则新开 queued 任务、
    用例 done → queued；仍 needs_* 则新落一条阻断任务（保留历史）。仅最近
    real 任务为环境阻断（done 且 env_check_result 为 needs_*）的用例可复检，
    其余状态 409。
    """
    case = db.get(TextCase, exec_case.text_case_id)
    if case is None:
        raise ExecuteConflict("not_found", "用例不存在")
    latest_real = db.execute(
        select(ExecutionTask)
        .where(
            ExecutionTask.executable_case_id == exec_case.id,
            ExecutionTask.execution_target == "real",
        )
        .order_by(ExecutionTask.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    blocked = latest_real is not None and latest_real.env_check_result in (
        "needs_create",
        "needs_modify",
    )
    if case.status != TextCaseStatus.DONE or not blocked:
        raise ExecuteConflict(
            "not_blocked", "仅环境校验阻断（done）的用例可发起复检"
        )
    outcome = lass.check_topology(case.required_topology)
    return _create_real_task(db, exec_case, case, outcome)
