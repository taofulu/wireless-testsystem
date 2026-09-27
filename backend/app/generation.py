"""generation 模块：结构化步骤渲染为可执行用例（T7，故事 12–15）。

职责（spec 模块划分 rendering/generation）：
- 准入闸门：仅 confirmed/generated 态可生成（confirmed 首次生成；
  generated 态内重开新版本，不产生状态迁移——spec 状态机注释）
- 组装纯函数渲染输入：从操作目录解析 op_name/kind/device_target/必填参数/
  kind 专属声明，映射为 rendering.RenderStep
- 版本只追加不覆盖：version = max+1（同用例内从 1 连续），历史版本保留
  （故事 15）；渲染失败（RenderError）抛出不落任何行——不产生空版本

代码只读边界（ADR-0001，架构性禁止）：本模块只写 code 列，系统不提供任何
代码编辑入口；上游修复只能改文本/映射后重新生成新版本。
"""
import threading
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.catalog import required_fields
from app.models import TextCase, TextCaseStatus
from app.models.catalog import Operation
from app.models.executable_case import ExecutableCase
from app.models.mapping import MappingStatus, StructuredStep
from app.rendering import RenderError, RenderStep, render_case

# 生成准入态：confirmed 首次生成；generated 态内重新生成（高频反复，状态不变）
_ALLOWED_STATES = (TextCaseStatus.CONFIRMED, TextCaseStatus.GENERATED)

# 串行化"读 max(version) → 插入 → commit"读改写段：并发生成同一用例时
# version = max+1 不再互相踩踏（唯一约束兜底 IntegrityError → 409）
_generate_lock = threading.Lock()


class GenerationConflict(Exception):
    """当前用例状态/步骤不满足生成前提；路由统一转 409。"""


def _kind_extra(extra: Optional[dict], key: str) -> Any:
    if isinstance(extra, dict):
        return extra.get(key)
    return None


def _build_render_steps(db: Session, case: TextCase) -> list[RenderStep]:
    """把落库步骤 + 操作目录声明组装为渲染输入；不满足前提即拒绝。"""
    steps = list(
        db.execute(
            select(StructuredStep)
            .where(StructuredStep.text_case_id == case.id)
            .order_by(StructuredStep.seq)
        ).scalars().all()
    )
    if not steps:
        raise GenerationConflict("没有可渲染的结构化步骤，请先完成映射与确认")

    render_steps: list[RenderStep] = []
    for step in steps:
        if step.aw_operation_id is None or step.mapping_status == MappingStatus.UNMAPPED.value:
            raise GenerationConflict(
                f"步骤 {step.seq} 尚未映射到 AW 操作，请回到确认态处理后重新确认"
            )
        op = db.get(Operation, step.aw_operation_id)
        if op is None:  # 确认闸门已保证，防御目录条目被移除的脏数据
            raise GenerationConflict(
                f"步骤 {step.seq} 引用的操作目录条目不存在，请回到确认态重新选择"
            )
        required = required_fields(op.params_schema)
        render_steps.append(
            RenderStep(
                seq=step.seq,
                action_text=step.action_text,
                params=step.params,
                assertion_text=step.assertion_text,
                op_name=op.name,
                op_kind=op.kind,
                op_target=op.device_target,
                required_params=required,
                suboperations=_kind_extra(op.extra, "suboperations") or (),
                produces_artifacts=bool(_kind_extra(op.extra, "produces_artifacts")),
            )
        )
    return render_steps


def generate_case(db: Session, case: TextCase) -> ExecutableCase:
    """渲染当前步骤并追加为新版本（POST /text-cases/{id}/generate）。

    - confirmed → generated；generated 保持不变（spec：重新 generate 不产生
      状态迁移）
    - 渲染为纯函数；RenderError 原样上抛（路由转 422），本函数在渲染成功前
      不写任何数据，保证"渲染失败不产生空版本"
    """
    if case.status not in _ALLOWED_STATES:
        raise GenerationConflict("仅确认后的用例（confirmed/generated）可生成可执行用例")

    render_steps = _build_render_steps(db, case)
    code = render_case(
        title=case.title,
        precondition=case.precondition,
        expected_text=case.expected_text,
        steps=render_steps,
    )  # RenderError 上抛即 422，此时尚未写库

    # 渲染（纯函数，慢）留在锁外；锁只保护版本号读改写与落库提交
    with _generate_lock:
        current = db.execute(
            select(ExecutableCase.version)
            .where(ExecutableCase.text_case_id == case.id)
            .order_by(ExecutableCase.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        version = (current or 0) + 1

        row = ExecutableCase(text_case_id=case.id, version=version, code=code)
        db.add(row)
        case.status = TextCaseStatus.GENERATED
        try:
            db.commit()
        except IntegrityError as exc:  # 多 worker/多进程并发兜底：唯一约束撞版本号
            db.rollback()
            raise GenerationConflict("并发生成冲突，请重试") from exc
        db.refresh(row)
        return row
