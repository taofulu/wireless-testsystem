"""evolution 路由：种子认证与用例进化（T14，故事 23–27；ADR-0008）。

链路：
- POST /text-cases/{id}/mark-seed：标记 origin=seed 并写入 variable_slots
- POST /text-cases/{id}/evolve：复制种子结构化步骤+文本，替换槽位占位符，
  落库 origin=evolved、parent_case_id 指向种子、status=mapped
- GET  /text-cases/{id}/evolutions：列出由种子进化产生的全部用例（血缘树）

进化过程零 LLM 成本、确定性可复现（ADR-0008）；追溯链五环由 trace 端点
按 parent_case_id 自动串联，无需额外接线。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.evolution import (
    EvolutionConflict,
    SlotValidationError,
    evolve_case,
    list_evolved,
    mark_seed,
)
from app.models import TextCase
from app.schemas import EvolveIn, MarkSeedIn, TextCaseOut, TextCaseSummaryOut

router = APIRouter(prefix="/text-cases", tags=["evolution"])


def _get_case_or_404(db: Session, case_id: int) -> TextCase:
    case = db.get(TextCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="text case not found")
    return case


@router.post("/{case_id}/mark-seed", response_model=TextCaseOut)
def post_mark_seed(
    case_id: int, payload: MarkSeedIn, db: Session = Depends(get_db)
):
    """标记用例为种子并声明可变参数槽位（故事 23）。

    MVP 手工认证：权限层后续票细化（spec Further Notes）。重提交整体替换
    槽位定义（不增量合并）。标记为种子后仍可走 generate/execute 流程。
    """
    case = _get_case_or_404(db, case_id)
    return mark_seed(db, case, payload.variable_slots)


@router.post("/{case_id}/evolve", response_model=TextCaseOut, status_code=201)
def post_evolve(
    case_id: int, payload: EvolveIn, db: Session = Depends(get_db)
):
    """从种子用例进化产生新用例（故事 24）。

    - 种子须 origin=seed 且 confirmed/generated 态、步骤全部 mapped
    - slot_values 必须恰好覆盖种子声明的全部槽位（多/缺 422 拒绝）
    - 复制种子的结构化步骤与文本，替换 ``{{slot_name}}`` 占位符为值
    - 新用例 origin=evolved、parent_case_id 指向种子、status=mapped、
      elaboration_qa/mapping_job 留 null（零 LLM 成本）
    """
    seed = _get_case_or_404(db, case_id)
    try:
        evolved = evolve_case(db, seed, payload.slot_values)
    except EvolutionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SlotValidationError as exc:
        raise HTTPException(
            status_code=422, detail={"code": exc.code, "detail": exc.detail}
        ) from exc
    return evolved


@router.get(
    "/{case_id}/evolutions", response_model=list[TextCaseSummaryOut]
)
def list_evolutions(case_id: int, db: Session = Depends(get_db)):
    """列出由种子进化产生的全部用例（故事 27 血缘树）。新的在前。"""
    _get_case_or_404(db, case_id)
    return list_evolved(db, case_id)
