"""生成路由：可执行用例生成、版本历史、只读代码查看（T7，故事 13–15）。

链路：UI → POST /text-cases/{id}/generate（渲染并追加新版本）→
GET /text-cases/{id}/executable-cases（版本历史）→ GET /executable-cases/{id}/code
（全文只读查看，故事 13）。

只读边界（ADR-0001 架构性禁止）：本路由对 /executable-cases/{id}/code 只注册
GET——PUT/PATCH/DELETE 等一律 405，系统不存在任何代码编辑入口（测试守护）。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.generation import GenerationConflict, generate_case
from app.models import ExecutableCase, TextCase
from app.rendering import RenderError
from app.schemas import ExecutableCaseOut, ExecutableCodeOut

router = APIRouter(tags=["generation"])


@router.post(
    "/text-cases/{case_id}/generate",
    response_model=ExecutableCaseOut,
    status_code=201,
)
def generate(case_id: int, db: Session = Depends(get_db)):
    """渲染当前结构化步骤并追加为新的可执行用例版本（故事 12/15）。

    - 仅 confirmed/generated 态可生成，其余状态 409
    - 渲染失败（缺参数等）返回 422 并携带步骤序号与原因，不产生空版本
    """
    case = db.get(TextCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="text case not found")
    try:
        row = generate_case(db, case)
    except GenerationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RenderError as exc:
        raise HTTPException(
            status_code=422,
            detail={"step_seq": exc.step_seq, "code": exc.code, "detail": exc.detail},
        ) from exc
    return row


@router.get(
    "/text-cases/{case_id}/executable-cases",
    response_model=list[ExecutableCaseOut],
)
def list_executable_cases(case_id: int, db: Session = Depends(get_db)):
    """版本历史（故事 15）：同一用例全部版本，新版本在前；只追加不覆盖。"""
    case = db.get(TextCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="text case not found")
    return list(
        db.execute(
            select(ExecutableCase)
            .where(ExecutableCase.text_case_id == case_id)
            .order_by(ExecutableCase.version.desc())
        ).scalars().all()
    )


@router.get("/executable-cases/{exec_id}/code", response_model=ExecutableCodeOut)
def get_code(exec_id: int, db: Session = Depends(get_db)):
    """代码全文只读查看（故事 13）。编辑入口不存在（见模块 docstring）。"""
    row = db.get(ExecutableCase, exec_id)
    if row is None:
        raise HTTPException(status_code=404, detail="executable case not found")
    return row
