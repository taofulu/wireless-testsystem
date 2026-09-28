"""reporting 模块：步骤级报告解析与五环追溯的纯函数层（T13）。

职责（spec 模块划分 reporting）：
- Allure 结果原文 → 逐步骤 pass/fail，映射回原始文本步骤序号（故事 33）
- 多次真实执行历史 → flaky 识别（故事 37）
- 沙盒报告显著区分于真实执行报告（故事 47，与 schemas 的声明常量协同）

纯函数：不触库、不取时间、不做 IO——同一输入永远产出同一输出。
DB 触达由路由层完成，本模块只接受已查得的载荷与结构化步骤视图。

Allure 步骤标题锚点（ADR-0004 + app.rendering）：渲染产出的 pytest 代码
把每个结构化步骤包进 ``with allure.step('步骤N: 动作文本'):``——序号 N 即
原始文本步骤序号。本模块据此把 Allure 报告中的步骤 title 解析回 seq，
失败点可秒级定位到原始文本第 N 步。
"""
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

# 渲染层产出的 Allure step 标题前缀（见 app.rendering.render_case）：
#   "步骤{seq}: {action_text}"
# 仅捕获前缀的整数 seq，action_text 由结构化步骤按 seq 回填。
_STEP_TITLE_RE = re.compile(r"^步骤\s*(\d+)\s*[:：]")


@dataclass(frozen=True)
class ParsedStep:
    """Allure 单步解析结果：映射回原始文本步骤序号。

    seq 为 None 表示标题不符合"步骤N:"锚点（如 setup/teardown 附属步骤），
    仍展示但不与原始文本步骤对应。
    """

    seq: Optional[int]
    title: str
    status: str
    # 由结构化步骤按 seq 回填的原始动作文本；seq 为 None 时为 None
    action_text: Optional[str]


@dataclass(frozen=True)
class FlakySummary:
    """同一用例多次真实执行的 flaky 识别（故事 37）。

    is_flaky：同时存在 passed 与 failed（非 env_failed）的多次执行——
    同一代码版本不同执行轮次结果不一致即 flaky。仅 1 次执行不判 flaky。
    """

    total_runs: int
    passed_count: int
    failed_count: int
    is_flaky: bool


def parse_step_seq(title: str) -> Optional[int]:
    """从 Allure step 标题解析原始文本步骤序号；不匹配返回 None。

    锚点见模块 docstring。容忍全角冒号与序号前后的空白。
    """
    match = _STEP_TITLE_RE.match(title.strip())
    if match is None:
        return None
    return int(match.group(1))


def parse_allure_steps(
    allure_report: Optional[Mapping[str, Any]],
    action_by_seq: Mapping[int, str],
) -> list[ParsedStep]:
    """Allure 结果原文 → 逐步骤映射回原始文本步骤序号（故事 33）。

    - allure_report 为 None/缺 steps：空列表（real 任务未回传或沙盒任务）
    - action_by_seq：原始文本步骤序号 → 动作文本，用于回填动作文本
    - 防御 Allure 形状：缺 title/status 的条目跳过，不抛错（报告不可控）
    """
    if not allure_report:
        return []
    raw_steps = allure_report.get("steps")
    if not isinstance(raw_steps, list):
        return []

    parsed: list[ParsedStep] = []
    for raw in raw_steps:
        if not isinstance(raw, Mapping):
            continue
        title = raw.get("title")
        status = raw.get("status")
        if not isinstance(title, str) or not isinstance(status, str):
            continue
        seq = parse_step_seq(title)
        action_text = action_by_seq.get(seq) if seq is not None else None
        parsed.append(
            ParsedStep(seq=seq, title=title, status=status, action_text=action_text)
        )
    return parsed


# 判决计为"代码失败"的取值：仅 failed（断言失败）。env_failed 是环境类失败
# （故事 53：场景文件不可达等），属环境问题而非代码 flaky——把它计入 flaky
# 会误导工程师查代码而非查环境，故排除。env_failed 仍计入 total_runs。
_FAIL_VERDICTS = frozenset({"failed"})


def identify_flaky(verdicts: Sequence[str]) -> FlakySummary:
    """多次真实执行判决序列 → flaky 识别（故事 37）。

    is_flaky 仅在至少 2 次执行且同时出现 passed 与 failed（断言失败）时为真——
    同代码版本同环境却时过时败即代码 flaky。env_failed 是环境类失败，不计为
    flaky（环境不稳 ≠ 代码 flaky，故事 53 的环境/代码分离不被此抹平）。
    """
    total = len(verdicts)
    passed = sum(1 for v in verdicts if v == "passed")
    failed = sum(1 for v in verdicts if v in _FAIL_VERDICTS)
    is_flaky = total >= 2 and passed >= 1 and failed >= 1
    return FlakySummary(
        total_runs=total,
        passed_count=passed,
        failed_count=failed,
        is_flaky=is_flaky,
    )
