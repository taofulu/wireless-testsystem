"""文本用例路由：三栏录入、草稿列表、继续编辑（T2 纵切片）。

链路：UI → /text-cases → persistence（ADR-0005 DB 唯一主存）。
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.elaboration import ElaborationConflict, apply_text_case_patch
from app.models import TextCase, TextCaseStatus
from app.schemas import TextCaseCreate, TextCaseOut, TextCasePatch

router = APIRouter(prefix="/text-cases", tags=["text-cases"])


@router.post("", response_model=TextCaseOut, status_code=201)
def create_text_case(payload: TextCaseCreate, db: Session = Depends(get_db)):
    """新建文本用例，初始状态 draft。"""
    case = TextCase(
        title=payload.title,
        precondition=payload.precondition,
        steps_text=payload.steps_text,
        expected_text=payload.expected_text,
        status=TextCaseStatus.DRAFT,
    )
    db.add(case)
    db.commit()
    db.refresh(case)
    return case


@router.get("", response_model=list[TextCaseOut])
def list_text_cases(
    status: Optional[TextCaseStatus] = None, db: Session = Depends(get_db)
):
    """用例列表：按创建时间倒序，最新在前；`?status=draft` 取草稿列表。"""
    stmt = select(TextCase).order_by(TextCase.created_at.desc(), TextCase.id.desc())
    if status is not None:
        stmt = stmt.where(TextCase.status == status)
    return db.execute(stmt).scalars().all()


@router.get("/{case_id}", response_model=TextCaseOut)
def get_text_case(case_id: int, db: Session = Depends(get_db)):
    case = db.get(TextCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="text case not found")
    return case


@router.patch("/{case_id}", response_model=TextCaseOut)
def patch_text_case(
    case_id: int, payload: TextCasePatch, db: Session = Depends(get_db)
):
    """继续编辑：更新提供的字段。

    状态不回退；但若扩写闸门已通过（sufficient/skipped）且评估输入被改动，
    闸门失效回退 answered，防止未评估文本直接流入映射（ADR-0007）。
    """
    case = db.get(TextCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="text case not found")

    updates = payload.model_dump(exclude_unset=True)
    try:
        apply_text_case_patch(case, updates)
    except ElaborationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    db.refresh(case)
    return case
