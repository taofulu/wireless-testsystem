"""执行路由：沙盒调试任务发起（T8，故事 38/45）。

POST /executable-cases/{id}/debug 为 generated 态用例创建沙盒执行任务；
沙盒 Worker（能力路由）领取后以真实 pytest 子进程执行。调试会话记录与
三态判决展示在 T9/T10。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.execution import DebugConflict, create_debug_task
from app.models import ExecutableCase
from app.schemas import ExecutionTaskOut

router = APIRouter(tags=["execution"])


@router.post(
    "/executable-cases/{exec_id}/debug",
    response_model=ExecutionTaskOut,
    status_code=201,
)
def debug(exec_id: int, db: Session = Depends(get_db)):
    """发起沙盒调试：generated 态内闭环，不迁移用例状态（ADR-0009）。"""
    exec_case = db.get(ExecutableCase, exec_id)
    if exec_case is None:
        raise HTTPException(status_code=404, detail="executable case not found")
    try:
        return create_debug_task(db, exec_case)
    except DebugConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
