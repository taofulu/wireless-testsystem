"""reporting 路由：步骤级报告与五环追溯（T13，故事 33/34/37/47）。

- GET /executable-cases/{id}/executions/{task_id}/report
    步骤级 pass/fail 报告：real 任务解析 Allure 映射回原始文本步骤序号（故事 33）；
    sandbox 任务取调试会话 step_results 并携带 environment_disclaimer（故事 47）
- GET /executable-cases/{id}/trace
    五环追溯视图（种子→进化→结构化→代码→结果），仅统计真实执行（故事 34/37）

沙盒调试会话不进五环追溯与正式执行历史（ADR-0009）——追溯的 executions 环
只查 real 任务，与 /executions 历史端点同源；flaky 识别仅基于真实执行判决。
"""
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import TextCase
from app.models.execution import ExecutionTask
from app.models.mapping import StructuredStep
from app.reporting import identify_flaky, parse_allure_steps
from app.routers.execution import _get_exec_or_404
from app.schemas import (
    AllureStepOut,
    ExecutableCaseOut,
    ExecutionRecordOut,
    ExecutionRingOut,
    FlakySummaryOut,
    SANDBOX_ENVIRONMENT_DISCLAIMER,
    StepReportOut,
    StructuredStepOut,
    TextCaseSummaryOut,
    TraceChainOut,
)

router = APIRouter(tags=["reporting"])


def _structured_steps(db: Session, text_case_id: int) -> list[StructuredStep]:
    """用例的结构化步骤按 seq 升序（步骤级报告 action_text 回填来源）。"""
    return list(
        db.execute(
            select(StructuredStep)
            .where(StructuredStep.text_case_id == text_case_id)
            .order_by(StructuredStep.seq)
        ).scalars().all()
    )


def _action_text_by_seq(steps: list[StructuredStep]) -> dict[int, str]:
    return {s.seq: s.action_text for s in steps}


def _sandbox_steps(
    step_results: list[Any], action_by_seq: dict[int, str]
) -> list[AllureStepOut]:
    """沙盒调试会话 step_results → 步骤级报告视图（故事 47）。

    step_results 形如 [{seq, sim_level, status, detail?}]（status: pass|fail|not_run），
    seq 直接对应原始文本步骤序号；action_text 由结构化步骤回填，与 real 通路一致。
    """
    out: list[AllureStepOut] = []
    for sr in step_results:
        if not isinstance(sr, dict):
            continue
        seq = sr.get("seq")
        status = sr.get("status")
        if not isinstance(seq, int) or not isinstance(status, str):
            continue
        action_text = action_by_seq.get(seq)
        title = f"步骤{seq}: {action_text}" if action_text else f"步骤{seq}"
        out.append(AllureStepOut(seq=seq, title=title, status=status, action_text=action_text))
    return out


@router.get(
    "/executable-cases/{exec_id}/executions/{task_id}/report",
    response_model=StepReportOut,
)
def step_report(exec_id: int, task_id: int, db: Session = Depends(get_db)):
    """步骤级报告（故事 33/47）。

    - real 任务：解析 Allure 结果原文，逐步骤映射回原始文本步骤序号，失败点秒级定位
    - sandbox 任务：取调试会话 step_results（仿真标注），并携带 environment_disclaimer
      （仿真执行未进行环境校验），与真实执行报告显著区分、不被误用为环境可用性证据
    - 任务不属于该用例或不存在 → 404；未回传结果 → 空步骤报告（执行中）
    """
    exec_case = _get_exec_or_404(db, exec_id)
    task = db.get(ExecutionTask, task_id)
    if task is None or task.executable_case_id != exec_case.id:
        raise HTTPException(status_code=404, detail="task not found")

    is_sandbox = task.execution_target == "sandbox"
    result = task.result
    if result is None:
        # 未回传：呈现空步骤报告（前端据此显示"执行中"）
        return StepReportOut(
            task_id=task.id,
            execution_target=task.execution_target,
            is_sandbox=is_sandbox,
            verdict="",
        )

    action_by_seq = _action_text_by_seq(_structured_steps(db, exec_case.text_case_id))
    if is_sandbox:
        steps = _sandbox_steps(result.step_results, action_by_seq)
        disclaimer: Optional[str] = SANDBOX_ENVIRONMENT_DISCLAIMER
    else:
        parsed = parse_allure_steps(result.allure_report, action_by_seq)
        steps = [
            AllureStepOut(
                seq=p.seq, title=p.title, status=p.status, action_text=p.action_text
            )
            for p in parsed
        ]
        disclaimer = None  # real 报告不带沙盒声明，显著区分（故事 47）

    return StepReportOut(
        task_id=task.id,
        execution_target=task.execution_target,
        is_sandbox=is_sandbox,
        verdict=result.verdict,
        environment_disclaimer=disclaimer,
        steps=steps,
        logs=result.logs,
        artifacts=result.artifacts,
        sim_package_version=result.sim_package_version,
        created_at=result.created_at,
    )


@router.get(
    "/executable-cases/{exec_id}/trace",
    response_model=TraceChainOut,
)
def trace(exec_id: int, db: Session = Depends(get_db)):
    """五环追溯视图（故事 34/37）：种子→进化→结构化→代码→结果，双向回查。

    - 种子/进化环：非进化路径（parent_case_id 为 null）下两环为 None；进化路径
      下 seed_case 为父用例、evolution_case 为当前用例本身（T14 落地后填入）
    - 结构化环：当前用例的结构化步骤（seq/action_text/操作/参数/断言）
    - 代码环：当前可执行用例版本
    - 结果环：仅真实执行历史（沙盒调试不进追溯），含 flaky 识别（故事 37）
    """
    exec_case = _get_exec_or_404(db, exec_id)
    case = db.get(TextCase, exec_case.text_case_id)
    if case is None:
        # 可执行用例存在则其文本用例必存在；防御性 404
        raise HTTPException(status_code=404, detail="text case not found")

    # 种子/进化环
    seed_case: Optional[TextCaseSummaryOut] = None
    evolution_case: Optional[TextCaseSummaryOut] = None
    if case.parent_case_id is not None:
        parent = db.get(TextCase, case.parent_case_id)
        if parent is not None:
            seed_case = TextCaseSummaryOut.model_validate(parent)
            evolution_case = TextCaseSummaryOut.model_validate(case)

    # 结构化环
    structured_out = [
        StructuredStepOut.model_validate(s) for s in _structured_steps(db, case.id)
    ]

    # 结果环：仅真实执行（沙盒调试不进追溯，ADR-0009）
    tasks = list(
        db.execute(
            select(ExecutionTask)
            .where(
                ExecutionTask.executable_case_id == exec_case.id,
                ExecutionTask.execution_target == "real",
            )
            .order_by(ExecutionTask.id.desc())
        ).scalars().all()
    )
    records = [ExecutionRecordOut.model_validate(t) for t in tasks]
    verdicts = [
        t.result.verdict for t in tasks if t.result is not None and t.result.verdict
    ]
    flaky = identify_flaky(verdicts)

    return TraceChainOut(
        executable_case_id=exec_case.id,
        seed_case=seed_case,
        evolution_case=evolution_case,
        structured_steps=structured_out,
        executable_case=ExecutableCaseOut.model_validate(exec_case),
        executions=ExecutionRingOut(
            records=records,
            flaky=FlakySummaryOut(
                total_runs=flaky.total_runs,
                passed_count=flaky.passed_count,
                failed_count=flaky.failed_count,
                is_flaky=flaky.is_flaky,
            ),
        ),
    )
