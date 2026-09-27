"""映射路由：触发映射、轮询作业、查看结构化步骤（T5，故事 7/8/9/49）。

链路：UI → /text-cases/{id}/map → GLM CLI 子进程（映射 skill）→ 服务端
字典二次校验 → persistence（ADR-0001/0002/0005/0006/0010）。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.mapping import MappingConflict, list_steps, start_mapping
from app.models import TextCase
from app.schemas import MappingJobOut, StructuredStepOut

router = APIRouter(prefix="/text-cases", tags=["mapping"])


def _get_case_or_404(db: Session, case_id: int) -> TextCase:
    case = db.get(TextCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="text case not found")
    return case


@router.post("/{case_id}/map", response_model=MappingJobOut, status_code=202)
def map_text_case(case_id: int, db: Session = Depends(get_db)):
    """一键触发映射：异步子进程，立即返回 running，前端轮询。"""
    case = _get_case_or_404(db, case_id)
    try:
        start_mapping(db, case)
    except MappingConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return case.mapping_job


@router.get("/{case_id}/mapping", response_model=MappingJobOut)
def get_mapping_job(case_id: int, db: Session = Depends(get_db)):
    """轮询映射作业状态（含步骤统计与字典拦截留痕）。"""
    case = _get_case_or_404(db, case_id)
    if case.mapping_job is None:
        raise HTTPException(status_code=404, detail="mapping not started")
    return case.mapping_job


@router.get("/{case_id}/steps", response_model=list[StructuredStepOut])
def get_structured_steps(case_id: int, db: Session = Depends(get_db)):
    """查看结构化步骤；未映射步骤 mapping_status=unmapped，供确认态（T6）高亮。"""
    _get_case_or_404(db, case_id)
    return list_steps(db, case_id)
