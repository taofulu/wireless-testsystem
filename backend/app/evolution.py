"""evolution 模块：参数化用例进化（T14，故事 23–27；ADR-0008）。

职责（spec 模块划分 evolution）：
- 种子认证：标记 origin=seed 并写入 variable_slots（名称→描述）。MVP 手工
  认证，权限层后续票细化（spec Further Notes）。重提交整体替换槽位定义。
- 用例进化：校验种子准入（origin=seed 且状态在 confirmed/generated，有
  结构化步骤）→ 校验槽位值完整性（必须覆盖全部声明的槽位，多余拒绝）→
  复制种子的结构化步骤与文本到新用例，把 ``{{slot_name}}`` 占位符替换为值
  → 新用例 origin=evolved、parent_case_id 指向种子、status=mapped、
  elaboration_qa/mapping_job 留空（绕过扩写+映射，零 LLM 成本）
- 追溯链五环自动串联：trace 端点已按 parent_case_id 取种子/进化两环，
  进化路径落库即串联，无需额外接线

边界（ADR-0008 + ADR-0001/0002 架构性原则）：
- 进化过程不调用 GLM、不产生新的映射不确定性——继承种子的映射结论
- 槽位占位符语法固定为 ``{{slot_name}}``（与 Jinja 一致，渲染层不参与）；
  若整段字符串等于 ``{{slot_name}}``，替换为原值（保留类型，便于 int/float
  参数）；否则做子串替换（结果恒为字符串）
- 新用例 status=mapped：用户可直接 confirm 推进至 generated（结构化步骤
  全部 mapped，继承自种子已确认结论），亦可先在确认态调整参数后确认
"""
import re
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TextCase, TextCaseOrigin, TextCaseStatus
from app.models.mapping import StructuredStep

# 种子准入态：confirmed（首次确认）或 generated（重开新版本）。这两个态
# 都意味着结构化步骤已存在且全部 mapped/manual；mapped 态尚未确认完，
# 未映射步骤会随进化复制过去——拒绝。draft/elaborating/queued/running/
# done 一律拒绝（无步骤或已进入执行域，不应作为进化基准）。
_SEED_ALLOWED_STATES = (TextCaseStatus.CONFIRMED, TextCaseStatus.GENERATED)

# 占位符正则：{{slot_name}}；容忍空白。名称须为合法 Python 标识符风格
# （字母数字下划线），避免把任意 JSON 字符串当成槽位。
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class EvolutionConflict(Exception):
    """当前用例状态/血缘不满足进化前提；路由统一转 409。"""


class SlotValidationError(Exception):
    """槽位值校验失败（缺失/多余/种子含未声明占位符）；路由转 422。

    与 EvolutionConflict（409）显式区分：409 = 用例状态/血缘不允许该操作；
    422 = 调用方提供的槽位值集合与种子声明不一致，是调用方输入问题。
    """

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# 占位符替换（纯函数，无 IO；同一输入永远产出同一输出）
# ---------------------------------------------------------------------------


def substitute(value: Any, slot_values: dict[str, Any]) -> Any:
    """递归把 ``{{slot_name}}`` 占位符替换为 slot_values 中的值。

    - 整段字符串等于 ``{{slot_name}}`` → 替换为原值（保留类型，便于
      int/float 参数流入 params 的 JSON 形状）
    - 字符串含 ``{{slot_name}}`` 子串 → 子串替换（结果恒为字符串）
    - dict/list 递归；其他类型原样返回
    - 未声明的占位符（``{{unknown}}``）原样保留——校验在 evolve 入口
      完成（slot_values 必须覆盖种子声明的全部槽位），运行时再次扫描
      只为防御种子侧标注了槽位却忘了在 slot_values 中提供
    """
    if isinstance(value, str):
        # 整段即占位符：保留类型（int/float/bool 等直接流入 params）
        m = _PLACEHOLDER_RE.fullmatch(value.strip())
        if m is not None and m.group(1) in slot_values:
            return slot_values[m.group(1)]
        # 子串替换：未声明的占位符原样保留（让漏标的种子在执行期暴露）
        def _sub(match: re.Match[str]) -> str:
            name = match.group(1)
            if name in slot_values:
                return str(slot_values[name])
            return match.group(0)

        return _PLACEHOLDER_RE.sub(_sub, value)
    if isinstance(value, dict):
        return {k: substitute(v, slot_values) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, slot_values) for v in value]
    return value


def _scan_placeholders(value: Any) -> set[str]:
    """递归收集 value 中所有 ``{{slot_name}}`` 占位符的名称。"""
    found: set[str] = set()
    if isinstance(value, str):
        found.update(m.group(1) for m in _PLACEHOLDER_RE.finditer(value))
    elif isinstance(value, dict):
        for v in value.values():
            found |= _scan_placeholders(v)
    elif isinstance(value, list):
        for v in value:
            found |= _scan_placeholders(v)
    return found


# ---------------------------------------------------------------------------
# 种子认证
# ---------------------------------------------------------------------------


def mark_seed(db: Session, case: TextCase, variable_slots: dict[str, str]) -> TextCase:
    """标记用例为种子（origin=seed）并写入槽位定义。

    AC：可将用例标记为种子（MVP 手工认证 origin=seed）并维护可变参数槽位
    定义（频段/功率/UE 数等）。重提交整体替换槽位定义——允许调整槽位集。
    标记为种子后仍可继续走 generate/execute 流程（种子本身也是一条用例）。
    """
    case.origin = TextCaseOrigin.SEED
    case.variable_slots = dict(variable_slots)
    db.commit()
    db.refresh(case)
    return case


# ---------------------------------------------------------------------------
# 用例进化
# ---------------------------------------------------------------------------


def _list_seed_steps(db: Session, seed_id: int) -> list[StructuredStep]:
    return list(
        db.execute(
            select(StructuredStep)
            .where(StructuredStep.text_case_id == seed_id)
            .order_by(StructuredStep.seq)
        ).scalars().all()
    )


def _validate_slot_values(
    seed: TextCase, slot_values: dict[str, Any]
) -> None:
    """slot_values 必须恰好覆盖种子声明的槽位（多/缺都拒绝）。"""
    declared = set((seed.variable_slots or {}).keys())
    provided = set(slot_values.keys())
    missing = declared - provided
    extra = provided - declared
    if missing:
        raise SlotValidationError(
            "missing_slot_values",
            f"缺少槽位值：{', '.join(sorted(missing))}（种子声明："
            f"{', '.join(sorted(declared)) if declared else '（无）'}）",
        )
    if extra:
        raise SlotValidationError(
            "unknown_slot_values",
            f"未声明的槽位值：{', '.join(sorted(extra))}（种子仅声明："
            f"{', '.join(sorted(declared)) if declared else '（无）'}）",
        )


def evolve_case(
    db: Session, seed: TextCase, slot_values: dict[str, Any]
) -> TextCase:
    """复制种子结构化步骤+文本，替换槽位占位符，落库新用例（故事 24）。

    新用例：
    - origin=evolved，parent_case_id=seed.id（追溯链五环自动串联）
    - title/precondition/steps_text/expected_text 复制并替换占位符
    - required_topology 原样继承（拓扑是环境元数据，与槽位无关；用户可后改）
    - elaboration_qa/mapping_job 留 null（绕过扩写+映射，零 LLM 成本）
    - status=mapped（继承种子的映射结论，用户可直接 confirm 推进）
    - 结构化步骤逐条复制：seq/action_text/aw_operation_id/params/
      assertion_text/mapping_status 全部沿用；params/action_text/assertion_text
      中的 ``{{slot_name}}`` 替换为 slot_values 值

    准入闸门（被测试守护）：
    - seed.origin 必须为 seed（必须先经手工认证）
    - seed.status 必须 ∈ {confirmed, generated}（步骤已确认且全部 mapped）
    - 种子必须有结构化步骤（无步骤的种子不可作进化基准）
    - slot_values 必须恰好覆盖种子声明的槽位（多/缺都 422 拒绝）
    """
    if seed.origin != TextCaseOrigin.SEED:
        raise EvolutionConflict(
            "仅 origin=seed 的种子用例可进化；请先调用 POST /text-cases/{id}/mark-seed"
        )
    if seed.status not in _SEED_ALLOWED_STATES:
        raise EvolutionConflict(
            f"种子用例须处于 confirmed/generated 态（当前 {seed.status.value}），"
            "请先完成映射与确认"
        )

    seed_steps = _list_seed_steps(db, seed.id)
    if not seed_steps:
        raise EvolutionConflict("种子用例无可复制的结构化步骤，请先完成映射")

    # 有未映射步骤的种子不允作进化基准——会把 unmapped 状态传染给所有进化用例
    unmapped = [s.seq for s in seed_steps if s.aw_operation_id is None]
    if unmapped:
        raise EvolutionConflict(
            f"种子用例存在未映射步骤（序号：{', '.join(map(str, unmapped))}），"
            "请先回确认态补全操作后再进化"
        )

    _validate_slot_values(seed, slot_values)

    # 防御：slot_values 已覆盖全部声明的槽位，但若种子步骤文本里出现
    # 未声明的 {{unknown}} 占位符，子串替换会原样保留——这是种子标注错误，
    # 此处显式拒绝避免脏数据流入进化用例（故事 26：种子质量是质量杠杆）
    all_placeholders: set[str] = set()
    all_placeholders |= _scan_placeholders(seed.title)
    for field in ("precondition", "steps_text", "expected_text"):
        all_placeholders |= _scan_placeholders(getattr(seed, field))
    for step in seed_steps:
        all_placeholders |= _scan_placeholders(step.action_text)
        all_placeholders |= _scan_placeholders(step.assertion_text)
        all_placeholders |= _scan_placeholders(step.params)
    undeclared = all_placeholders - set((seed.variable_slots or {}).keys())
    if undeclared:
        raise SlotValidationError(
            "undeclared_placeholders",
            f"种子含未声明的占位符：{', '.join(sorted(undeclared))}，"
            "请先在 mark-seed 中补全槽位定义",
        )

    # 复制文本（替换占位符）
    evolved = TextCase(
        title=substitute(seed.title, slot_values),
        precondition=substitute(seed.precondition, slot_values),
        steps_text=substitute(seed.steps_text, slot_values),
        expected_text=substitute(seed.expected_text, slot_values),
        required_topology=seed.required_topology,
        status=TextCaseStatus.MAPPED,
        origin=TextCaseOrigin.EVOLVED,
        parent_case_id=seed.id,
    )
    db.add(evolved)
    db.flush()  # 取 evolved.id 用于挂结构化步骤

    for step in seed_steps:
        db.add(
            StructuredStep(
                text_case_id=evolved.id,
                seq=step.seq,
                action_text=substitute(step.action_text, slot_values),
                aw_operation_id=step.aw_operation_id,
                params=substitute(step.params, slot_values),
                assertion_text=substitute(step.assertion_text, slot_values),
                mapping_status=step.mapping_status,
            )
        )

    db.commit()
    db.refresh(evolved)
    return evolved


def list_evolved(db: Session, seed_id: int) -> list[TextCase]:
    """列出由 seed_id 进化产生的全部用例（血缘追溯列表用，故事 27）。

    按 origin=evolved + parent_case_id 双过滤——直接落库设置 parent_case_id
    但未标 origin=evolved 的用例（如 T13 的 trace 测试夹具）不在此列：
    本端点只关心真正的进化血缘，T13 的 trace 端点单独消费 parent_case_id。

    新的在前（id desc 兼顾同毫秒创建的并发场景）。
    """
    return list(
        db.execute(
            select(TextCase)
            .where(
                TextCase.parent_case_id == seed_id,
                TextCase.origin == TextCaseOrigin.EVOLVED,
            )
            .order_by(TextCase.created_at.desc(), TextCase.id.desc())
        ).scalars().all()
    )
