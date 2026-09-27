"""Worker 路由：注册/保活/能力路由领取/任务心跳/结果回传（T8，ADR-0003/0009）。

链路：Worker 启动 POST /worker/register（声明能力）→ 周期
POST /worker/heartbeat 保活 → 轮询 POST /worker/tasks/claim（按能力过滤，
204 表示空队列）→ 执行中 POST /worker/tasks/{id}/heartbeat 续约 →
POST /worker/tasks/{id}/result 回传。
"""
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.execution import (
    TaskConflict,
    TaskNotFound,
    UnknownWorker,
    claim_task,
    heartbeat_task,
    heartbeat_worker,
    register_worker,
    submit_result,
)
from app.schemas import (
    TaskClaimOut,
    TaskResultIn,
    TaskResultOut,
    WorkerClaimIn,
    WorkerHeartbeatIn,
    WorkerOut,
    WorkerRegisterIn,
)

router = APIRouter(tags=["worker"])


@router.post("/worker/register", response_model=WorkerOut)
def register(payload: WorkerRegisterIn, db: Session = Depends(get_db)):
    """Worker 启动注册；同 worker_id 重复注册视为续约更新（重启即最新声明）。"""
    return register_worker(db, payload)


@router.post("/worker/heartbeat", response_model=WorkerOut)
def heartbeat(payload: WorkerHeartbeatIn, db: Session = Depends(get_db)):
    """Worker 保活心跳；未注册（或已超时摘除）返回 404，Worker 应重新注册。"""
    try:
        return heartbeat_worker(db, payload.worker_id)
    except UnknownWorker as exc:
        raise HTTPException(status_code=404, detail="worker not registered") from exc


@router.post(
    "/worker/tasks/claim",
    response_model=TaskClaimOut,
    responses={204: {"description": "没有匹配能力的可领任务"}},
)
def claim(payload: WorkerClaimIn, db: Session = Depends(get_db)):
    """按能力过滤领取任务：先到先得、幂等（重试返回同一在领任务）。

    领取前摘除心跳超时的 Worker 并回收其在领任务（崩溃任务可被重领）。
    """
    try:
        task = claim_task(db, payload.worker_id, payload.capabilities)
    except UnknownWorker as exc:
        raise HTTPException(status_code=404, detail="worker not registered") from exc
    except TaskConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if task is None:
        return Response(status_code=204)
    return task


@router.post("/worker/tasks/{task_id}/heartbeat", response_model=TaskClaimOut)
def task_heartbeat(task_id: int, payload: WorkerHeartbeatIn, db: Session = Depends(get_db)):
    """任务执行中心跳：续约防回收；非持有者心跳 409。"""
    try:
        return heartbeat_task(db, task_id, payload.worker_id)
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    except UnknownWorker as exc:
        raise HTTPException(status_code=404, detail="worker not registered") from exc
    except TaskConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/worker/tasks/{task_id}/result", response_model=TaskResultOut, status_code=201
)
def result(task_id: int, payload: TaskResultIn, db: Session = Depends(get_db)):
    """结果回传：仅持有者可回传；重复回传 409（Worker 视为已记录）。"""
    try:
        return submit_result(db, task_id, payload)
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    except UnknownWorker as exc:
        raise HTTPException(status_code=404, detail="worker not registered") from exc
    except TaskConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
