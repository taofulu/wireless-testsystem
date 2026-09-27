"""扩写路由：触发评估、轮询状态、逐条回答、强制跳过（T4，故事 3–6）。

链路：UI → /text-cases/{id}/elaboration* → GLM CLI 子进程 → persistence
（ADR-0005 DB 唯一主存；ADR-0006 子进程异步轮询；ADR-0007 扩写前置）。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.elaboration import (
    ElaborationConflict,
    StaleAnswers,
    skip_elaboration,
    start_elaboration,
    submit_answers,
)
from app.models import TextCase
from app.schemas import ElaborationAnswersIn, ElaborationOut, TextCaseOut

router = APIRouter(prefix="/text-cases", tags=["elaboration"])


def _get_case_or_404(db: Session, case_id: int) -> TextCase:
    case = db.get(TextCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="text case not found")
    return case


@router.post("/{case_id}/elaborate", response_model=ElaborationOut, status_code=202)
def elaborate(case_id: int, db: Session = Depends(get_db)):
    """触发扩写评估：异步子进程，立即返回 running，前端轮询。"""
    case = _get_case_or_404(db, case_id)
    try:
        start_elaboration(db, case)
    except ElaborationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return case.elaboration_qa


@router.get("/{case_id}/elaboration", response_model=ElaborationOut)
def get_elaboration(case_id: int, db: Session = Depends(get_db)):
    """轮询扩写作业状态（含 missing_points 与多轮历史）。"""
    case = _get_case_or_404(db, case_id)
    if case.elaboration_qa is None:
        raise HTTPException(status_code=404, detail="elaboration not started")
    return case.elaboration_qa


@router.post("/{case_id}/elaboration/answers", response_model=TextCaseOut)
def answer_elaboration(
    case_id: int, payload: ElaborationAnswersIn, db: Session = Depends(get_db)
):
    """逐条回答本轮追问：答案合并回原文形成新版本。"""
    case = _get_case_or_404(db, case_id)
    try:
        submit_answers(db, case, payload)
    except ElaborationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except StaleAnswers as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return case


@router.post("/{case_id}/elaboration/skip", response_model=TextCaseOut)
def skip_elaboration_entry(case_id: int, db: Session = Depends(get_db)):
    """强制跳过扩写（接受质量风险），跳过后允许进入映射。"""
    case = _get_case_or_404(db, case_id)
    try:
        skip_elaboration(db, case)
    except ElaborationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return case
