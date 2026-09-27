"""确认态路由：步骤编辑与确认闸门（T6，故事 8–12、51、55）。

链路：UI → PATCH /text-cases/{id}/steps（编辑操作/参数）→ POST /confirm
（全部映射后确认）→ persistence（ADR-0002/0010）。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.confirmation import ConfirmationConflict, StepEditError, confirm_case, update_steps
from app.db import get_db
from app.models import TextCase
from app.schemas import ConfirmationOut, StepsPatchIn, StructuredStepOut

router = APIRouter(prefix="/text-cases", tags=["confirmation"])


def _get_case_or_404(db: Session, case_id: int) -> TextCase:
    case = db.get(TextCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="text case not found")
    return case


@router.patch("/{case_id}/steps", response_model=list[StructuredStepOut])
def patch_steps(case_id: int, payload: StepsPatchIn, db: Session = Depends(get_db)):
    """确认态编辑步骤的操作与参数（故事 10/11）。

    - 未映射步骤手选操作 → 状态变 manual
    - 已映射步骤可改操作（变 manual）或仅改参数（保留 mapped）
    - mml_generic 操作过命令字典服务端二次校验；场景类操作冻结
      scenario_id+scenario_version
    - 任一步校验失败整批拒绝（422），不落半更新
    """
    case = _get_case_or_404(db, case_id)
    try:
        steps = update_steps(db, case, payload.steps)
    except ConfirmationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except StepEditError as exc:
        raise HTTPException(
            status_code=422,
            detail={"step_id": exc.step_id, "code": exc.code, "detail": exc.detail},
        ) from exc
    return steps


@router.post("/{case_id}/confirm", response_model=ConfirmationOut)
def confirm(case_id: int, db: Session = Depends(get_db)):
    """确认闸门：全部步骤已映射后进入 confirmed 态（故事 12）。

    存在 unmapped 步骤或操作引用为空时返回 409，不改变状态（API 闸门被测试守护）。
    """
    case = _get_case_or_404(db, case_id)
    try:
        result = confirm_case(db, case)
    except ConfirmationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result
