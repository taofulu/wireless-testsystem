"""执行路由：沙盒调试任务、调试会话、仿真覆盖概览、真实执行闸门与执行历史。

- POST /executable-cases/{id}/debug          发起沙盒调试（可带调试预设，故事 40）
- GET  /executable-cases/{id}/debug-runs     最近 N 次调试会话（故事 46）
- GET  /executable-cases/{id}/sandbox-meta   逐步骤仿真覆盖概览（故事 41/55）
- GET  /executable-cases/{id}/sandbox-context Worker 内核执行上下文（供给+预设）
- POST /executable-cases/{id}/execute        提交真实执行（T10 二次确认闸门 →
                                             T11 LASS 三值环境校验闸门）
- POST /executable-cases/{id}/recheck     环境复检（故事 22：阻断后闭环；
                                             路径以 spec API 契约为准）
- GET  /executable-cases/{id}/executions     真实执行历史（T12 结果页数据源；
                                             正式执行历史只查 real，spec 数据模型）

沙盒调试在 generated 态内闭环（ADR-0009）：发起/回传均不迁移用例状态；
只有 execute/recheck 放行才把用例推进 queued，real 领取/回传推进
running/done（T12）。
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import lass
from app.config import settings
from app.db import get_db
from app.execution import (
    DebugConflict,
    ExecuteConflict,
    create_debug_task,
    recheck_env,
    submit_real_execution,
)
from app.models import ExecutableCase
from app.models.execution import ExecutionTask
from app.sandbox import build_sandbox_context, list_debug_runs, sandbox_meta
from app.schemas import (
    DebugIn,
    DebugRunListOut,
    DebugRunOut,
    ExecuteIn,
    ExecutionRecordOut,
    ExecutionTaskOut,
    SandboxContextOut,
    SandboxMetaOut,
)

router = APIRouter(tags=["execution"])


def _get_exec_or_404(db: Session, exec_id: int) -> ExecutableCase:
    exec_case = db.get(ExecutableCase, exec_id)
    if exec_case is None:
        raise HTTPException(status_code=404, detail="executable case not found")
    return exec_case


@router.post(
    "/executable-cases/{exec_id}/debug",
    response_model=ExecutionTaskOut,
    status_code=201,
)
def debug(
    exec_id: int, payload: Optional[DebugIn] = None, db: Session = Depends(get_db)
):
    """发起沙盒调试：generated 态内闭环，不迁移用例状态（ADR-0009）。

    body 可选携带调试预设（虚拟设备初始状态）；预设仅存于调试会话上下文，
    不写入用例正式数据（故事 40）。
    """
    exec_case = _get_exec_or_404(db, exec_id)
    try:
        return create_debug_task(db, exec_case, preset=(payload or DebugIn()).preset)
    except DebugConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get(
    "/executable-cases/{exec_id}/debug-runs",
    response_model=DebugRunListOut,
)
def debug_runs(exec_id: int, db: Session = Depends(get_db)):
    """同一代码版本最近 N 次调试会话（N 为配置项，故事 46；新的在前）。"""
    exec_case = _get_exec_or_404(db, exec_id)
    runs = list_debug_runs(db, exec_case.id)
    return DebugRunListOut(
        keep_latest=settings.debug_run_keep_latest,
        runs=[DebugRunOut.model_validate(r) for r in runs],
    )


@router.get(
    "/executable-cases/{exec_id}/sandbox-meta",
    response_model=SandboxMetaOut,
)
def meta(exec_id: int, db: Session = Depends(get_db)):
    """逐步骤仿真覆盖概览：确认态即可预告沙盒结论可信度（故事 41/55）。"""
    exec_case = _get_exec_or_404(db, exec_id)
    return sandbox_meta(db, exec_case)


@router.get(
    "/executable-cases/{exec_id}/sandbox-context",
    response_model=SandboxContextOut,
)
def sandbox_context(
    exec_id: int, task_id: Optional[int] = None, db: Session = Depends(get_db)
):
    """Worker 沙盒内核执行上下文：逐步骤仿真供给 + 调试预设。

    携带 task_id 时取该沙盒任务的调试预设；不带（前端预览）预设为空。
    任务不属于该用例或不是沙盒任务时 404（上下文不得跨任务泄漏）。
    """
    exec_case = _get_exec_or_404(db, exec_id)
    preset = None
    if task_id is not None:
        task = db.get(ExecutionTask, task_id)
        if (
            task is None
            or task.executable_case_id != exec_case.id
            or task.execution_target != "sandbox"
        ):
            raise HTTPException(status_code=404, detail="sandbox task not found")
        preset = task.preset
    return build_sandbox_context(db, exec_case, preset)


@router.post(
    "/executable-cases/{exec_id}/execute",
    response_model=ExecutionTaskOut,
    status_code=201,
)
def execute(exec_id: int, payload: ExecuteIn, db: Session = Depends(get_db)):
    """提交真实执行（T10 沙盒闸门 → T11 LASS 三值环境校验；故事 17-21、43/44）。

    - 最近调试会话判决 passed：进入 LASS 校验
    - 判决 inconclusive / 尚无调试结论：须带 confirm_inconclusive=true 二次
      确认，否则 409 并附未覆盖步骤清单
    - 判决 failed：一律 409——只许回上游修复并重新生成新版本（ADR-0009）
    - LASS ready：入队（generated → queued），任务带 env_check_result=ready
    - LASS needs_create/needs_modify：阻断——任务转 done 并附缺失清单/差异
      说明，用例转 done；环境中台处理后经 /env-recheck 闭环（故事 22）
    - LASS 未配置/不可达：503（校验未发生，不产生任何环境结论）
    """
    exec_case = _get_exec_or_404(db, exec_id)
    try:
        return submit_real_execution(db, exec_case, payload.confirm_inconclusive)
    except ExecuteConflict as exc:
        detail: dict = {"code": exc.code, "detail": exc.detail}
        if exc.uncovered is not None:
            detail["uncovered_steps"] = exc.uncovered
        raise HTTPException(status_code=409, detail=detail) from exc
    except lass.LassUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post(
    "/executable-cases/{exec_id}/recheck",
    response_model=ExecutionTaskOut,
    status_code=201,
)
def env_recheck(exec_id: int, db: Session = Depends(get_db)):
    """环境复检（故事 22）：环境中台处理后重新校验，ready 则正常入队。

    仅最近 real 任务为环境阻断（done + env_check_result 为 needs_*）的用例
    可复检，其余状态 409；LASS 不可达 503。
    """
    exec_case = _get_exec_or_404(db, exec_id)
    try:
        return recheck_env(db, exec_case)
    except ExecuteConflict as exc:
        raise HTTPException(
            status_code=409, detail={"code": exc.code, "detail": exc.detail}
        ) from exc
    except lass.LassUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get(
    "/executable-cases/{exec_id}/executions",
    response_model=list[ExecutionRecordOut],
)
def list_executions(exec_id: int, db: Session = Depends(get_db)):
    """该代码版本的真实执行历史（新的在前；结果页/追溯数据源）。

    只查 real 任务（spec：正式执行历史与五环追溯只查 real）——沙盒调试会话
    走 /debug-runs，不混入正式历史。
    """
    exec_case = _get_exec_or_404(db, exec_id)
    return list(
        db.execute(
            select(ExecutionTask)
            .where(
                ExecutionTask.executable_case_id == exec_case.id,
                ExecutionTask.execution_target == "real",
            )
            .order_by(ExecutionTask.id.desc())
        ).scalars().all()
    )
