"""confirmation 模块：确认态人工审核（T6；ADR-0002 未映射不猜测 / ADR-0010）。

职责（spec 模块划分 confirmation，故事 8–12、51、55）：
- 编辑任意步骤的 AW 操作与参数：未映射手选变 manual，改操作也变 manual；
  仅改参数且操作不变时保留原 mapped 状态（LLM 选的操作仍可追溯）
- 信任边界：mml_generic 操作的 {command, args} 必须过命令字典服务端二次校验
  （与映射落库同闸门，不信任人工手填的命令）；其他操作按 params_schema 校验
  必填字段；composite 场景类操作 params 冻结 {scenario_id, scenario_version}
- 确认闸门：全部步骤 mapping_status ∈ {mapped, manual} 且 aw_operation_id 非空
  才允许 confirmed；存在 unmapped 步骤一律拒绝（API 闸门被测试守护）

状态接缝：用例在 mapped 态进入确认态；编辑不改变用例状态；确认成功才推进到
confirmed（spec 8 态状态机）。
"""
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog import MMLCheck, check_generic_mml, operation_to_out
from app.models import TextCase, TextCaseStatus
from app.models.catalog import Operation, Scenario
from app.models.mapping import MappingStatus, StructuredStep
from app.schemas import StepPatchIn


class ConfirmationConflict(Exception):
    """当前用例状态不允许该操作；路由统一转 409。"""


class StepEditError(Exception):
    """步骤编辑校验失败（操作不存在/字典拦截/参数非法）；路由转 422。"""

    def __init__(self, step_id: int, code: str, detail: str):
        super().__init__(f"step {step_id}: {detail}")
        self.step_id = step_id
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# 参数校验：轻量 JSON Schema 必填字段校验 + 操作专属闸门
# ---------------------------------------------------------------------------


def _required_fields(params_schema: dict[str, Any]) -> list[str]:
    """从操作目录条目的 params_schema 提取必填字段列表。"""
    required = params_schema.get("required")
    if isinstance(required, list):
        return [str(f) for f in required]
    return []


def _validate_required_params(
    op: Operation, params: dict[str, Any]
) -> Optional[StepEditError]:
    """校验 params 包含 params_schema 声明的全部必填字段。

    不做完整 JSON Schema 校验（MVP 无 jsonschema 依赖）；必填字段是最低
    保障，类型/范围等由 mml_generic 字典校验与沙盒/真实执行期进一步拦截。
    """
    missing = [f for f in _required_fields(op.params_schema) if f not in params]
    if missing:
        return StepEditError(
            0,
            "missing_required_params",
            f"操作 {op.name} 缺少必填参数：{', '.join(missing)}",
        )
    return None


def _validate_scenario_params(params: dict[str, Any]) -> Optional[StepEditError]:
    """场景类操作（play_scenario 等 composite）参数冻结 scenario_id+version。

    两个字段必须为非空字符串；场景不必在索引中存在（无索引时手工录入可保存，
    播放时再校验可达性，属环境类失败）。
    """
    sid = params.get("scenario_id")
    ver = params.get("scenario_version")
    if not isinstance(sid, str) or not sid.strip():
        return StepEditError(0, "bad_scenario_params", "scenario_id 必须为非空字符串")
    if not isinstance(ver, str) or not ver.strip():
        return StepEditError(0, "bad_scenario_params", "scenario_version 必须为非空字符串")
    return None


def _validate_mml_generic(
    db: Session, params: dict[str, Any]
) -> Optional[StepEditError]:
    """mml_generic 操作：params 形状 {command, args} 且命令字典校验通过。

    与映射落库同闸门（check_generic_mml），人工手填的命令同样不信任。
    """
    command = params.get("command")
    args = params.get("args")
    if not isinstance(command, str) or not command.strip():
        return StepEditError(0, "bad_mml_params", "params 需含非空 command 字符串")
    if not isinstance(args, dict):
        return StepEditError(0, "bad_mml_params", "params 需含 args 对象")
    check: MMLCheck = check_generic_mml(db, command, args)
    if not check.ok:
        return StepEditError(0, check.code, check.detail or "命令字典校验失败")
    return None


def _validate_step_params(
    db: Session, op: Operation, params: dict[str, Any]
) -> Optional[StepEditError]:
    """按操作 kind 分派参数校验。"""
    if op.kind == "mml_generic":
        return _validate_mml_generic(db, params)
    # composite 场景类操作（play_scenario）：scenario_id+version 必填
    if op.kind == "composite":
        err = _validate_scenario_params(params)
        if err is not None:
            return err
    # 通用必填字段校验（覆盖 mml_family/long_running/instrument_primitive 等）
    return _validate_required_params(op, params)


# ---------------------------------------------------------------------------
# 步骤编辑
# ---------------------------------------------------------------------------


def _resolve_operation(db: Session, op_id: int) -> Operation:
    op = db.get(Operation, op_id)
    if op is None:
        raise StepEditError(0, "unknown_operation", f"操作目录中不存在 id={op_id} 的条目")
    return op


def _apply_step_patch(db: Session, case: TextCase, patch: StepPatchIn) -> None:
    """校验并应用单步编辑（操作引用 + 参数）。"""
    step = db.get(StructuredStep, patch.id)
    if step is None or step.text_case_id != case.id:
        raise StepEditError(patch.id, "step_not_found", "步骤不存在或不属于当前用例")

    clearing = (
        patch.aw_operation_id is None and "aw_operation_id" in patch.model_fields_set
    )

    # 确定目标操作：
    # - 显式提供非空 aw_operation_id → 目标操作（需校验）
    # - 不提供 aw_operation_id（仅改参数）→ 沿用当前操作（需校验）
    # - 显式提供 null（清空操作）→ 无目标操作，不校验
    target_op: Optional[Operation] = None
    if patch.aw_operation_id is not None:
        target_op = _resolve_operation(db, patch.aw_operation_id)
    elif not clearing and step.aw_operation_id is not None:
        target_op = db.get(Operation, step.aw_operation_id)

    # 参数：patch 提供则用新值，否则保留
    params = patch.params if patch.params is not None else step.params

    # 有目标操作时校验参数；清空操作不校验
    if target_op is not None:
        err = _validate_step_params(db, target_op, params)
        if err is not None:
            raise StepEditError(patch.id, err.code, err.detail)

    # 状态迁移：
    # - 显式提供 aw_operation_id → 人工改了操作引用 → manual
    #   （无论原状态是 mapped 还是 unmapped；改操作即人工干预）
    # - 仅改参数（不提供 aw_operation_id）→ 保留原 mapped/manual 状态
    # - aw_operation_id 显式 null → 清空操作 → unmapped（允许撤销误选）
    if patch.aw_operation_id is not None:
        step.aw_operation_id = patch.aw_operation_id
        step.mapping_status = MappingStatus.MANUAL.value
    elif clearing:
        step.aw_operation_id = None
        step.mapping_status = MappingStatus.UNMAPPED.value
    # else: 仅 params 变更，状态不变

    step.params = params


def update_steps(db: Session, case: TextCase, patches: list[StepPatchIn]) -> list[StructuredStep]:
    """确认态批量编辑步骤（PATCH /text-cases/{id}/steps）。

    准入：用例必须处于 mapped 态（确认态）。逐条校验，任一条失败整批回滚
    （原子性，避免半更新状态）。成功后返回更新后的步骤列表（按 seq 升序）。
    """
    if case.status != TextCaseStatus.MAPPED:
        raise ConfirmationConflict("仅 mapped 态用例可在确认态编辑步骤")

    # 先全部校验再落库，保证原子性
    for patch in patches:
        _apply_step_patch(db, case, patch)
    db.commit()

    return list(
        db.execute(
            select(StructuredStep)
            .where(StructuredStep.text_case_id == case.id)
            .order_by(StructuredStep.seq)
        ).scalars().all()
    )


# ---------------------------------------------------------------------------
# 确认闸门
# ---------------------------------------------------------------------------


def confirm_case(db: Session, case: TextCase) -> dict[str, Any]:
    """确认全部步骤已映射 → 用例进入 confirmed 态（POST /text-cases/{id}/confirm）。

    闸门（被测试守护）：
    - 用例必须处于 mapped 态
    - 全部步骤 mapping_status ∈ {mapped, manual}（无 unmapped）
    - 全部步骤 aw_operation_id 非空
    任一不满足返回 409，不改变状态。
    """
    if case.status != TextCaseStatus.MAPPED:
        raise ConfirmationConflict("仅 mapped 态用例可确认")

    steps = list(
        db.execute(
            select(StructuredStep)
            .where(StructuredStep.text_case_id == case.id)
            .order_by(StructuredStep.seq)
        ).scalars().all()
    )
    if not steps:
        raise ConfirmationConflict("无可确认的结构化步骤，请先完成映射")

    unmapped = [
        s for s in steps
        if s.mapping_status == MappingStatus.UNMAPPED.value or s.aw_operation_id is None
    ]
    if unmapped:
        seqs = ", ".join(str(s.seq) for s in unmapped)
        raise ConfirmationConflict(
            f"存在未映射步骤（序号：{seqs}），请先为所有步骤选择操作后再确认"
        )

    case.status = TextCaseStatus.CONFIRMED
    db.commit()

    manual_count = sum(
        1 for s in steps if s.mapping_status == MappingStatus.MANUAL.value
    )
    return {
        "status": case.status.value,
        "step_count": len(steps),
        "manual_count": manual_count,
    }
