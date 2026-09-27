"""sandbox 模块：仿真供给装配、三态判决聚合、调试会话记录（T9/T10）。

职责（spec 模块划分 sandbox，ADR-0009）：
- 仿真供给装配：按用例结构化步骤从操作目录取出 simulatable/sim_ref，
  declarative 效果描述符从 catalog_dir 内联（Worker 无需访问 catalog 目录），
  python 供给以 module:function 引用下发（Worker 侧 --sim-package-dir 加载）
- 有效仿真级别聚合（纯函数）：composite 取子操作最差者；场景类操作按场景
  索引 has_meta 降级 unsimulated；declarative 描述符缺失同样降级
- 三态判决聚合（纯函数）：任一 fail → failed；否则任一非 simulated →
  inconclusive；否则 passed。Worker 内核内置同一规则（独立包不依赖后端），
  两侧由各自的纯函数单测守护同一判定表
- 调试会话记录：sandbox 任务回传时落 debug_run（含 preset 快照与
  sim_package_version），并按配置保留最近 N 次（应用层清理）

仿真实现所有权归 AW 团队（随 catalog 一起交付），本模块只装配与解释供给，
不手写任何操作的行为仿真。
"""
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.catalog import Operation, Scenario
from app.models.debug import DebugRun
from app.models.executable_case import ExecutableCase
from app.models.execution import ExecutionTask
from app.models.mapping import StructuredStep

logger = logging.getLogger(__name__)

# 仿真级别（CONTEXT.md）：simulated（declarative/python 有状态仿真）>
# schema_stub（仅签名/参数校验）> unsimulated（未仿真）
SIM_LEVELS = ("simulated", "schema_stub", "unsimulated")
_LEVEL_RANK = {"simulated": 2, "schema_stub": 1, "unsimulated": 0}

# simulatable 声明 → 供给级别（none 即未仿真；declarative/python 为有状态仿真）
_DECLARED_LEVEL = {
    "declarative": "simulated",
    "python": "simulated",
    "schema_stub": "schema_stub",
    "none": "unsimulated",
}


# ---------------------------------------------------------------------------
# 纯函数：级别聚合与三态判决
# ---------------------------------------------------------------------------


def worst_level(levels: Sequence[str]) -> str:
    """组合操作有效级别：取子操作最差者（任一 unsimulated 则整步不可判定）。

    空序列按 unsimulated 处理（组合操作没有可仿真子操作即无可判定依据）。
    """
    if not levels:
        return "unsimulated"
    return min(levels, key=lambda lv: _LEVEL_RANK[lv])


def aggregate_verdict(step_results: Sequence[dict]) -> str:
    """三态判决（ADR-0009；与 Worker 侧 wts_worker.sandbox 同一判定表）：

    - 任一步骤 fail/not_run → failed（断言失败、AW 报错或步骤未执行到——
      缺执行证据按失败处理，诚实优先）
    - 否则任一步骤仅桩校验/未仿真 → inconclusive（禁止假绿）
    - 否则 → passed（全部有仿真实现且断言全过）
    - 空步骤列表 → failed（没有任何执行证据）
    """
    if not step_results:
        return "failed"
    if any(s.get("status") in ("fail", "not_run") for s in step_results):
        return "failed"
    if any(s.get("sim_level") != "simulated" for s in step_results):
        return "inconclusive"
    return "passed"


def uncovered_steps(step_results: Sequence[dict]) -> list[int]:
    """未覆盖步骤序号清单（仅桩校验/未仿真）——inconclusive 二次确认的依据。"""
    return sorted(
        int(s["seq"]) for s in step_results if s.get("sim_level") != "simulated"
    )


# ---------------------------------------------------------------------------
# 仿真供给装配（有效级别聚合）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubStepSim:
    """组合操作子操作的仿真供给视图。"""

    name: str
    simulatable: str
    descriptor: Optional[dict] = None
    sim_ref: Optional[str] = None


@dataclass(frozen=True)
class StepSim:
    """单步仿真供给视图（sandbox-context / sandbox-meta 共用）。"""

    seq: int
    op_name: str
    kind: str
    device_target: str
    simulatable: str  # 有效级别（已聚合 composite 最差者/场景元信息门）
    params_schema: dict = field(default_factory=dict)
    descriptor: Optional[dict] = None
    sim_ref: Optional[str] = None
    produces_artifacts: bool = False
    suboperations: tuple[SubStepSim, ...] = field(default_factory=tuple)


def _load_descriptor(sim_ref: Optional[str]) -> Optional[dict]:
    """从 catalog_dir 读取 declarative 效果描述符；缺失/非法返回 None。

    读取失败不抛错——诚实降级为 unsimulated（供给缺口如实呈现，不假绿）。
    """
    if not sim_ref:
        return None
    path = Path(settings.catalog_dir) / sim_ref
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("效果描述符不可读，步骤降级为未仿真: %s (%s)", path, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("效果描述符不是 JSON 对象，步骤降级为未仿真: %s", path)
        return None
    return data


def _declared_sim(op: Operation) -> tuple[str, Optional[dict]]:
    """单个目录条目的声明级别与内联描述符（declarative 缺失即降级）。"""
    level = _DECLARED_LEVEL.get(op.simulatable, "unsimulated")
    descriptor: Optional[dict] = None
    if op.simulatable == "declarative":
        descriptor = _load_descriptor(op.sim_ref)
        if descriptor is None:
            level = "unsimulated"
    return level, descriptor


def _scenario_meta_available(db: Session, params: dict) -> bool:
    """场景类步骤的元信息门：场景索引存在且 has_meta 才可声明式仿真。

    索引缺失（MBB 无 API 降级手工录入、或未同步）同样判不可用——诚实标记
    "必须真实 testbed 验证"（ADR-0010 consequences）。
    """
    scenario_id = params.get("scenario_id")
    scenario_version = params.get("scenario_version")
    if not isinstance(scenario_id, str) or not isinstance(scenario_version, str):
        return False
    row = db.execute(
        select(Scenario.has_meta).where(
            Scenario.scenario_id == scenario_id,
            Scenario.version == scenario_version,
        )
    ).first()
    return bool(row and row[0])


def build_step_sims(db: Session, exec_case: ExecutableCase) -> list[StepSim]:
    """装配可执行用例全部步骤的仿真供给（含有效级别聚合）。

    渲染输入同源：从结构化步骤 + 操作目录解析；操作条目被移除的脏数据
    按 unsimulated 诚实呈现（生成闸门已保证存在，此处为防御）。
    """
    steps = list(
        db.execute(
            select(StructuredStep)
            .where(StructuredStep.text_case_id == exec_case.text_case_id)
            .order_by(StructuredStep.seq)
        ).scalars().all()
    )

    sims: list[StepSim] = []
    for step in steps:
        op = db.get(Operation, step.aw_operation_id) if step.aw_operation_id else None
        if op is None:
            sims.append(
                StepSim(
                    seq=step.seq,
                    op_name="(未知操作)",
                    kind="",
                    device_target="",
                    simulatable="unsimulated",
                )
            )
            continue

        declared, descriptor = _declared_sim(op)
        sub_sims: tuple[SubStepSim, ...] = ()
        effective = declared
        if op.kind == "composite":
            parts: list[SubStepSim] = []
            for name in (op.extra or {}).get("suboperations") or []:
                sub_op = db.execute(
                    select(Operation).where(Operation.name == name)
                ).scalar_one_or_none()
                if sub_op is None:
                    # 子操作不在目录：无仿真供给依据，按最差级处理
                    parts.append(SubStepSim(name=name, simulatable="unsimulated"))
                    continue
                sub_level, sub_desc = _declared_sim(sub_op)
                parts.append(
                    SubStepSim(
                        name=name,
                        simulatable=sub_level,
                        descriptor=sub_desc,
                        sim_ref=sub_op.sim_ref if sub_op.simulatable == "python" else None,
                    )
                )
            sub_sims = tuple(parts)
            # 仿真级别取子操作最差者（ADR-0010；组合自身声明不参与聚合）
            effective = worst_level([p.simulatable for p in parts])
            # 场景元信息门：场景类组合操作无元信息即诚实标未仿真
            if effective == "simulated" and not _scenario_meta_available(db, step.params):
                effective = "unsimulated"

        sims.append(
            StepSim(
                seq=step.seq,
                op_name=op.name,
                kind=op.kind,
                device_target=op.device_target,
                simulatable=effective,
                params_schema=op.params_schema or {},
                descriptor=descriptor,
                sim_ref=op.sim_ref if op.simulatable == "python" else None,
                produces_artifacts=bool((op.extra or {}).get("produces_artifacts")),
                suboperations=sub_sims,
            )
        )
    return sims


def build_sandbox_context(
    db: Session, exec_case: ExecutableCase, preset: Optional[dict]
) -> dict[str, Any]:
    """Worker 内核执行上下文：逐步骤仿真供给 + 调试预设。"""
    sims = build_step_sims(db, exec_case)
    return {
        "executable_case_id": exec_case.id,
        "preset": preset,
        "steps": [
            {
                "seq": s.seq,
                "op_name": s.op_name,
                "kind": s.kind,
                "device_target": s.device_target,
                "simulatable": s.simulatable,
                "params_schema": s.params_schema,
                "descriptor": s.descriptor,
                "sim_ref": s.sim_ref,
                "produces_artifacts": s.produces_artifacts,
                "suboperations": [
                    {
                        "name": p.name,
                        "simulatable": p.simulatable,
                        "descriptor": p.descriptor,
                        "sim_ref": p.sim_ref,
                    }
                    for p in s.suboperations
                ],
            }
            for s in sims
        ],
    }


def sandbox_meta(db: Session, exec_case: ExecutableCase) -> dict[str, Any]:
    """逐步骤仿真覆盖概览（故事 41/55）：前端据此预告沙盒可信度。"""
    sims = build_step_sims(db, exec_case)
    return {
        "executable_case_id": exec_case.id,
        "steps": [
            {"seq": s.seq, "op_name": s.op_name, "simulatable": s.simulatable}
            for s in sims
        ],
        "has_uncovered": any(s.simulatable != "simulated" for s in sims),
    }


# ---------------------------------------------------------------------------
# 调试会话记录与保留策略
# ---------------------------------------------------------------------------


def record_debug_run(
    db: Session,
    task: ExecutionTask,
    *,
    verdict: str,
    step_results: list,
    sim_package_version: Optional[str],
) -> DebugRun:
    """为已完成的 sandbox 任务落一条调试会话，并按配置清理同版本旧会话。

    preset 快照从任务行复制（任务行是队列语义，debug_run 是业务记录）；
    sim_package_version 取执行 Worker 的注册声明（不信任回传自报）。
    """
    run = DebugRun(
        executable_case_id=task.executable_case_id,
        task_id=task.id,
        verdict=verdict,
        step_results=step_results,
        preset=task.preset,
        sim_package_version=sim_package_version,
    )
    db.add(run)
    db.flush()

    keep = settings.debug_run_keep_latest
    stale_ids = db.execute(
        select(DebugRun.id)
        .where(DebugRun.executable_case_id == task.executable_case_id)
        .order_by(DebugRun.created_at.desc(), DebugRun.id.desc())
        .offset(keep)
    ).scalars().all()
    if stale_ids:
        db.execute(delete(DebugRun).where(DebugRun.id.in_(stale_ids)))
    db.commit()
    db.refresh(run)
    return run


def list_debug_runs(db: Session, exec_case_id: int) -> list[DebugRun]:
    """同一代码版本最近 N 次调试会话（新的在前；故事 46）。"""
    return list(
        db.execute(
            select(DebugRun)
            .where(DebugRun.executable_case_id == exec_case_id)
            .order_by(DebugRun.created_at.desc(), DebugRun.id.desc())
            .limit(settings.debug_run_keep_latest)
        ).scalars().all()
    )


def latest_debug_run(db: Session, exec_case_id: int) -> Optional[DebugRun]:
    """该代码版本最近一次调试会话（真实执行闸门的判决依据）。"""
    return db.execute(
        select(DebugRun)
        .where(DebugRun.executable_case_id == exec_case_id)
        .order_by(DebugRun.created_at.desc(), DebugRun.id.desc())
        .limit(1)
    ).scalar_one_or_none()
