"""rendering 模块：结构化步骤 → AW pytest 代码（T7 纯函数层）。

spec 模块划分中 rendering 的职责就是纯函数：输入结构化步骤（含操作目录
声明），输出带 Allure step 标记的 pytest 文件全文。不触库、不取时间、
不做 IO——同一输入永远产出同一代码（快照测试守护）。

模板分支按 ADR-0010 五类操作 kind 选择：

- ``mml_family``         → ``aw.{target}.{op}(**params)``              高频命令族的类型化 API
- ``mml_generic``        → ``aw.{target}.run_mml(command=..., args=...)`` 通用 MML 执行原语
- ``long_running``       → 调用 + ``aw.wait_completion`` (+ ``aw.collect_artifacts``)
- ``instrument_primitive`` → ``aw.{target}.{op}(**params)``            仪表原语调用
- ``composite``          → 单次调用 + 子操作序列注释（对外一步骤↔一操作↔一报告映射，
                           子操作不展开为独立 Allure step）

约定（ADR-0001/0004）：每个结构化步骤渲染为一个 ``allure.step`` 块，块内含
AW 调用与断言，保证步骤级报告与原始文本步骤一一对应；文件头声明代码由系
统渲染产出、禁止手工编辑（系统不提供任何代码编辑入口）。

渲染失败（缺必填参数、mml_generic 形状非法、composite 场景引用缺失、未知
kind 等）抛出 :class:`RenderError`，由生成路由转 422——不产生空版本。
"""
from dataclasses import dataclass, field
from textwrap import indent
from typing import Any, Mapping, Optional, Sequence

# device_target → AW 框架设备命名空间（目录 schema 只允许这四个取值）
# 注意：取值集与 app.schemas 的 DeviceTarget Literal 保持同步
_TARGET_NAMESPACE = {"bbu": "bbu", "ue": "ue", "instrument": "instrument", "mbb": "mbb"}

# 五类操作 kind（ADR-0010）；取值集与 app.schemas 的 OperationKind Literal 保持同步
_KINDS = (
    "mml_family",
    "mml_generic",
    "long_running",
    "instrument_primitive",
    "composite",
)


class RenderError(Exception):
    """渲染失败（明确原因）；生成路由转 422，不落任何版本。"""

    def __init__(self, code: str, detail: str, step_seq: Optional[int] = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.step_seq = step_seq


@dataclass(frozen=True)
class RenderStep:
    """渲染输入的单条结构化步骤（与 ORM 解耦的轻量视图）。

    op_* 字段由生成层从操作目录解析后填入；required_params 取自
    params_schema.required（缺失即渲染失败）；suboperations/produces_artifacts
    为 kind 专属声明。
    """

    seq: int
    action_text: str
    params: Mapping[str, Any] = field(default_factory=dict)
    assertion_text: str = ""
    op_name: str = ""
    op_kind: str = ""
    op_target: str = ""
    required_params: Sequence[str] = ()
    suboperations: Sequence[str] = ()
    produces_artifacts: bool = False


# ---------------------------------------------------------------------------
# 纯函数渲染器
# ---------------------------------------------------------------------------


def _doc_sanitize(text: str) -> str:
    """用户文本嵌入 docstring 的转义：反斜杠与三引号不破坏字符串边界。"""
    return text.replace("\\", "\\\\").replace('"""', '""\\"')


def _indented(text: str) -> str:
    """docstring 中的栏目文本：去首尾空行后统一缩进 4 空格。"""
    stripped = text.strip("\n")
    if not stripped.strip():
        return "    （未填写）"
    return indent(stripped, "    ")


def _test_name(title: str) -> str:
    """标题 → pytest 函数名：仅保留 ASCII 字母数字，中文标题回退 test_case。"""
    slug = "".join(c.lower() if c.isascii() and c.isalnum() else "_" for c in title)
    slug = "_".join(part for part in slug.split("_") if part) or "case"
    return f"test_{slug}"


def _normalized_repr(value: Any) -> str:
    """确定性 repr：递归把 dict 键排序（JSON 键序不定，repr 保留插入序）。

    list/tuple 逐项归一；标量原样。保证"同逻辑参数 → 同渲染输出"。
    """
    if isinstance(value, dict):
        items = ", ".join(
            f"{_normalized_repr(k)}: {_normalized_repr(v)}" for k, v in sorted(value.items())
        )
        return "{" + items + "}"
    if isinstance(value, (list, tuple)):
        items = ", ".join(_normalized_repr(item) for item in value)
        return "[" + items + "]" if isinstance(value, list) else "(" + items + ")"
    return repr(value)


def _kwarg_pairs(params: Mapping[str, Any]) -> list[str]:
    """params → 排序后的 ``key=repr(value)`` 片段（键序确定保证渲染确定性）。"""
    pairs: list[str] = []
    for key in sorted(params):
        if not key.isidentifier():
            raise RenderError("bad_param_name", f"参数名 {key!r} 不是合法标识符")
        pairs.append(f"{key}={_normalized_repr(params[key])}")
    return pairs


def _render_call(step: RenderStep) -> str:
    """按 kind 选择模板，返回步骤的 AW 调用语句（可多行，已含缩进）。"""
    namespace = _TARGET_NAMESPACE.get(step.op_target)
    if namespace is None:
        raise RenderError(
            "unknown_device_target",
            f"操作 {step.op_name} 的 device_target {step.op_target!r} 不在框架命名空间内",
            step.seq,
        )

    missing = [name for name in step.required_params if name not in step.params]
    if missing:
        raise RenderError(
            "missing_required_params",
            f"操作 {step.op_name} 缺少必填参数：{', '.join(missing)}",
            step.seq,
        )

    lines: list[str] = []
    if step.op_kind == "mml_family":
        call = f"aw.{namespace}.{step.op_name}({', '.join(_kwarg_pairs(step.params))})"
    elif step.op_kind == "mml_generic":
        command = step.params.get("command")
        args = step.params.get("args")
        if not isinstance(command, str) or not command.strip():
            raise RenderError(
                "bad_mml_params", "mml_generic 步骤 params 需含非空 command 字符串", step.seq
            )
        if not isinstance(args, dict):
            raise RenderError(
                "bad_mml_params", "mml_generic 步骤 params 需含 args 对象", step.seq
            )
        call = (
            f"aw.{namespace}.run_mml("
            f"command={command!r}, args={_normalized_repr(args)})"
        )
    elif step.op_kind == "long_running":
        call = f"aw.{namespace}.{step.op_name}({', '.join(_kwarg_pairs(step.params))})"
        lines.append(f"        result_{step.seq} = {call}")
        lines.append(f"        aw.wait_completion(result_{step.seq})")
        if step.produces_artifacts:
            lines.append(
                f"        aw.collect_artifacts(result_{step.seq})"
                "  # 制品：testbed 侧路径与校验和"
            )
        return "\n".join(lines)
    elif step.op_kind == "instrument_primitive":
        call = f"aw.{namespace}.{step.op_name}({', '.join(_kwarg_pairs(step.params))})"
    elif step.op_kind == "composite":
        if step.suboperations:
            chain = " → ".join(step.suboperations)
            lines.append(f"        # 有序子操作（对外一个报告映射）: {chain}")
        call = f"aw.{namespace}.{step.op_name}({', '.join(_kwarg_pairs(step.params))})"
    else:
        raise RenderError(
            "unknown_kind",
            f"操作 {step.op_name} 的 kind {step.op_kind!r} 没有对应渲染模板",
            step.seq,
        )

    lines.append(f"        result_{step.seq} = {call}")
    return "\n".join(lines)


def render_case(
    title: str,
    precondition: str,
    expected_text: str,
    steps: Sequence[RenderStep],
) -> str:
    """纯函数：用例元信息 + 结构化步骤 → pytest 文件全文。

    任一步骤无法渲染时抛 :class:`RenderError`（携带步骤序号与原因）；
    步骤列表为空同样拒绝——生成层据此保证不产生空版本。
    """
    if not steps:
        raise RenderError("empty_steps", "没有可渲染的结构化步骤，请先完成映射")

    body_blocks: list[str] = []
    for step in steps:
        if step.op_kind not in _KINDS:
            raise RenderError(
                "unknown_kind",
                f"操作 {step.op_name} 的 kind {step.op_kind!r} 没有对应渲染模板",
                step.seq,
            )
        call_block = _render_call(step)
        if step.assertion_text.strip():
            assert_line = (
                f"        assert result_{step.seq}.ok, "
                f"{f'步骤{step.seq} 断言: {step.assertion_text}'!r}"
            )
        else:
            assert_line = f"        assert result_{step.seq}.ok"
        body_blocks.append(
            "\n".join(
                [
                    f"    with allure.step({f'步骤{step.seq}: {step.action_text}'!r}):",
                    f"        # AW 操作: {step.op_name} ({step.op_kind}/{step.op_target})",
                    call_block,
                    assert_line,
                ]
            )
        )

    header = "\n".join(
        [
            f'"""{_doc_sanitize(title)}',
            "",
            "由 Wireless Test System 模板渲染自动生成，禁止手工编辑（ADR-0001）：",
            "代码 100% 由结构化步骤渲染产出，修改请回上游文本/确认态后重新生成。",
            "",
            "预知条件:",
            _indented(_doc_sanitize(precondition)),
            "",
            "预期结果:",
            _indented(_doc_sanitize(expected_text)),
            '"""',
        ]
    )

    decorators = "\n".join(
        [
            "@allure.parent_suite('Wireless Test System')",
            f"@allure.suite({title!r})",
        ]
    )
    return "\n".join(
        [
            header,
            "import allure",
            "",
            "import aw",
            "",
            "",
            decorators,
            f"def {_test_name(title)}() -> None:",
            # 闭合三引号独立成行：标题以单个 " 结尾时，单行格式会拼出
            # 四连引号破坏字符串边界（SyntaxError）；换行后无该相邻性
            f'    """{_doc_sanitize(title)}\n    """',
            *body_blocks,
        ]
    ) + "\n"
