"""mapping 模块：文本用例映射为结构化步骤（T5）。

职责（spec 模块划分，ADR-0001/0002/0006/0010）：
- 消费扩写闸门（elaboration_qa.state 为 sufficient/skipped 才放行）
- 组装临时工作目录注入文件：input.md（用例文本）、catalog_subset.json
  （候选操作条目，携带 kind/device_target；mml_generic 挂当前生效字典的
  命令片段）、terms.json（术语子集，术语库供给前为空集占位）、
  output_schema.json；以子进程异步调用映射 skill，轮询取结果
- 落库前服务端对账（不信任 LLM 自报合法）：mapped 引用的操作必须真实存在；
  mml_generic 的 {command, args} 必须通过命令字典二次校验，命令缺失/参数
  非法一律降级 unmapped，进确认态（T6）高亮手选
- 成功后整批替换旧步骤并置用例 mapped；CLI 超时/失败/坏输出落 failed，
  不动旧步骤与用例状态，可重试

状态接缝：映射不新增用例状态（spec 8 态固定）。作业细粒度状态存于
text_case.mapping_job（running/succeeded/failed）。
"""
import json
import shutil
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm.exc import StaleDataError

from app import glm_cli
from app.catalog import MMLCheck, check_generic_mml, operation_to_out
from app.config import settings
from app.db import SessionLocal
from app.elaboration import TEXT_FIELDS, render_input_md
from app.glm_cli import CLIJobError
from app.models import TextCase, TextCaseStatus
from app.models.catalog import CommandDictionaryEntry, DictionaryState, Operation
from app.models.mapping import MappingStatus, StructuredStep
from app.schemas import MappingCLIResult, MappingCLIStepIn

# 扩写闸门通过态：只有这两个状态允许进入映射（spec 171 行）
_GATE_PASSED_STATES = ("sufficient", "skipped")


class MappingConflict(Exception):
    """当前用例/作业状态不允许该操作；路由统一转 409。"""


class BadMappingResult(Exception):
    """CLI 输出结构自相矛盾（空步骤已由边界模型挡、序号乱序等）；作业落 bad_result。"""


# ---------------------------------------------------------------------------
# 输出 schema（注入给 CLI 的形状与后端验收模型同源，不允许双份手写漂移）
# ---------------------------------------------------------------------------


def build_output_schema() -> dict[str, Any]:
    schema = MappingCLIResult.model_json_schema()
    schema["$schema"] = "http://json-schema.org/draft-07/schema#"
    return schema


# ---------------------------------------------------------------------------
# 候选集预筛注入
# ---------------------------------------------------------------------------


def build_catalog_subset(db: Session) -> list[dict[str, Any]]:
    """构造 catalog_subset.json：候选条目携带 kind/device_target（AC 明列）。

    MVP 注入操作目录全量（条目量尚小，真实预筛策略随术语库到位后迭代）；
    mml_generic 条目只额外挂"当前生效字典版本"的命令片段而非其他无关数据
    （spec 175 行/ADR-0010：仅注入相关命令族字典片段）。字典缺失/未导入时
    commands 为空列表——注入侧可以诚实给空，落库前的服务端二次校验才是
    唯一闸门（字典缺失期所有通用 MML 调用一律 unmapped）。
    """
    ops = db.execute(select(Operation).order_by(Operation.id)).scalars().all()
    state = db.get(DictionaryState, 1)
    dict_commands: list[dict[str, Any]] = []
    if state is not None:
        rows = db.execute(
            select(CommandDictionaryEntry).where(
                CommandDictionaryEntry.version == state.active_version
            )
        ).scalars().all()
        dict_commands = [
            {"command": row.command, "params": row.params} for row in rows
        ]

    subset: list[dict[str, Any]] = []
    for op in ops:
        entry = operation_to_out(op)
        if op.kind == "mml_generic":
            entry["dictionary_version"] = state.active_version if state else None
            entry["commands"] = dict_commands
        subset.append(entry)
    return subset


def _inject_workdir(db: Session, case: TextCase, workdir: Path) -> None:
    """按 ADR-0006 契约写入注入文件。"""
    case_view = {"title": case.title}
    case_view.update({field: getattr(case, field) for field in TEXT_FIELDS})
    (workdir / "input.md").write_text(render_input_md(case_view), encoding="utf-8")
    (workdir / "catalog_subset.json").write_text(
        json.dumps(build_catalog_subset(db), ensure_ascii=False), encoding="utf-8"
    )
    # 术语库 repo 尚未 pin commit 供给（spec Further Notes），先以空集占位；
    # 仍是 ADR-0006 契约必选项，fake CLI 会校验其存在性。
    (workdir / "terms.json").write_text("[]", encoding="utf-8")
    (workdir / "output_schema.json").write_text(
        json.dumps(build_output_schema(), ensure_ascii=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# 服务端对账：信任边界（ADR-0002/0010，只降级不升级）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Reclassified:
    """LLM 自报 mapped 被服务端降级为 unmapped 的留痕。"""

    seq: int
    code: str
    detail: Optional[str]


def validate_step_sequence(steps: list[MappingCLIStepIn]) -> None:
    """步骤序号必须从 1 开始、连续且不重复（与原始文本步骤一一对应）。"""
    seqs = [step.seq for step in steps]
    expected = list(range(1, len(steps) + 1))
    if seqs != expected:
        raise BadMappingResult(f"步骤序号必须从 1 连续且不重复，实际为 {seqs}")


def _check_generic_mml_params(db: Session, params: dict[str, Any]) -> Optional[MMLCheck]:
    """校验 mml_generic 步骤 params 形状并做命令字典服务端二次校验。

    返回 None 表示通过；否则返回拦截结论（code 供确认态提示分类）。
    """
    command = params.get("command")
    args = params.get("args")
    if not isinstance(command, str) or not command:
        return MMLCheck(False, "bad_mml_params", "params 需含非空 command 字符串")
    if not isinstance(args, dict):
        return MMLCheck(False, "bad_mml_params", "params 需含 args 对象")
    check = check_generic_mml(db, command, args)
    return None if check.ok else check


def reconcile_steps(
    db: Session, raw_steps: list[MappingCLIStepIn]
) -> tuple[list[tuple[MappingStatus, Optional[int], dict[str, Any]]], list[Reclassified]]:
    """把 CLI 原始步骤对账为可落库行。

    - unmapped 以状态为准：即使携带操作引用也清空（不允许夹带未确认映射）
    - mapped 缺引用/引用不存在 → 降级 unknown_operation/missing_operation
    - mapped mml_generic：命令字典二次校验不过 → 降级，code 透传字典校验码
      （unknown_command/bad_type/out_of_range/dictionary_missing/...）
    被降级步骤保留原始 params（如 LLM 尝试的 command/args），为确认态手选
    提供线索；action_text/断言原文始终保留。
    """
    validate_step_sequence(raw_steps)

    ids = {step.aw_operation_id for step in raw_steps if step.aw_operation_id is not None}
    ops: dict[int, Operation] = {}
    if ids:
        ops = {
            op.id: op
            for op in db.execute(
                select(Operation).where(Operation.id.in_(ids))
            ).scalars().all()
        }

    rows: list[tuple[MappingStatus, Optional[int], dict[str, Any]]] = []
    reclassified: list[Reclassified] = []
    for step in raw_steps:
        status = MappingStatus.MAPPED
        op_id = step.aw_operation_id

        if step.mapping_status == "unmapped":
            status = MappingStatus.UNMAPPED
            op_id = None
        elif op_id is None:
            status = MappingStatus.UNMAPPED
            reclassified.append(
                Reclassified(step.seq, "missing_operation", "mapped 步骤缺少 aw_operation_id")
            )
        else:
            op = ops.get(op_id)
            if op is None:
                status = MappingStatus.UNMAPPED
                reclassified.append(
                    Reclassified(
                        step.seq,
                        "unknown_operation",
                        f"操作目录中不存在 id={op_id} 的条目",
                    )
                )
                op_id = None
            elif op.kind == "mml_generic":
                check = _check_generic_mml_params(db, step.params)
                if check is not None:
                    status = MappingStatus.UNMAPPED
                    reclassified.append(
                        Reclassified(step.seq, check.code, check.detail)
                    )
                    op_id = None

        rows.append((status, op_id, step.params))
    return rows, reclassified


# ---------------------------------------------------------------------------
# 作业状态流转
# ---------------------------------------------------------------------------

# 进程内触发准入（同 elaboration：MVP 单 uvicorn 进程，进程锁消除并发双击；
# DB 中 state=running 是崩溃恢复权威依据）。
_trigger_lock = threading.Lock()
_running_jobs: dict[int, str] = {}
_active_procs: dict[str, subprocess.Popen] = {}


def is_mapping_running(case: TextCase) -> bool:
    """映射作业进行中（供草稿编辑守卫：注入输入不得在作业期间变化）。"""
    return bool(case.mapping_job and case.mapping_job.get("state") == "running")


def _new_job(state: str, job_token: Optional[str] = None) -> dict[str, Any]:
    return {
        "state": state,
        "error": None,
        "job_token": job_token,
        "step_count": None,
        "mapped_count": None,
        "unmapped_count": None,
        "reclassifications": [],
    }


def start_mapping(db: Session, case: TextCase) -> None:
    """校验扩写闸门 → 置 running → 起子进程线程（立即返回，可轮询）。

    elaborating（首次映射）与 mapped（故事 14：改文本重新映射经闸门后重试）
    均可触发；其余状态一律拒绝。running 期间状态留在原处——成功才置 mapped。
    """
    qa = case.elaboration_qa
    if qa is None or qa.get("state") not in _GATE_PASSED_STATES:
        raise MappingConflict("扩写闸门尚未通过，请先完成扩写评估或强制跳过")
    if case.status not in (TextCaseStatus.ELABORATING, TextCaseStatus.MAPPED):
        raise MappingConflict("当前用例状态不允许触发映射")

    with _trigger_lock:
        if case.id in _running_jobs:
            raise MappingConflict("映射正在进行中")
        if case.mapping_job is not None and case.mapping_job.get("state") == "running":
            raise MappingConflict("映射正在进行中")

        token = uuid.uuid4().hex
        case.mapping_job = _new_job("running", token)
        db.commit()
        _running_jobs[case.id] = token

    thread = threading.Thread(
        target=_run_mapping_job,
        args=(case.id, token),
        name=f"wts-mapping-{case.id}",
        daemon=True,
    )
    thread.start()


def _commit_job(db: Session, case: TextCase) -> bool:
    """提交 mapping_job 变更（JSON 列需 flag_modified），带令牌陈旧守卫。

    与 elaboration._commit_qa 同理：旧作业迟到的收尾不得覆盖新一轮作业；
    行已不属于本会话时回滚（连带同事务内的步骤替换一起撤销）。
    """
    job = case.mapping_job or {}
    token = job.get("job_token")
    if token is not None:
        table = TextCase.__table__
        stmt = select(table.c.mapping_job).where(table.c.id == case.id)
        with db.no_autoflush:
            row = db.execute(stmt).first()
        if row is None or (row[0] or {}).get("job_token") != token:
            db.rollback()
            return False

    flag_modified(case, "mapping_job")
    try:
        db.commit()
    except StaleDataError:
        db.rollback()
        return False
    return True


def _mark_failed(db: Session, case: TextCase, code: str, detail: str) -> bool:
    """落明确失败态：不动旧步骤、不回退/推进用例状态，可重试。"""
    job = case.mapping_job
    job["state"] = "failed"
    job["error"] = {"code": code, "detail": detail[:1000]}
    job["reclassifications"] = []
    return _commit_job(db, case)


def _persist_steps(
    db: Session, case: TextCase, parsed: MappingCLIResult
) -> Optional[dict[str, Any]]:
    """对账 → 整批替换结构化步骤 → 置 mapped；单事务原子提交。"""
    rows, reclassified = reconcile_steps(db, parsed.steps)

    db.execute(delete(StructuredStep).where(StructuredStep.text_case_id == case.id))
    for step, (status, op_id, params) in zip(parsed.steps, rows):
        db.add(
            StructuredStep(
                text_case_id=case.id,
                seq=step.seq,
                action_text=step.action_text,
                aw_operation_id=op_id,
                params=params,
                assertion_text=step.assertion_text,
                mapping_status=status.value,
            )
        )

    mapped_count = sum(1 for status, _id, _params in rows if status == MappingStatus.MAPPED)
    job = case.mapping_job
    job.update(
        {
            "state": "succeeded",
            "error": None,
            "step_count": len(rows),
            "mapped_count": mapped_count,
            "unmapped_count": len(rows) - mapped_count,
            "reclassifications": [
                {"seq": item.seq, "code": item.code, "detail": item.detail}
                for item in reclassified
            ],
        }
    )
    case.status = TextCaseStatus.MAPPED
    if not _commit_job(db, case):
        return None
    return case.mapping_job


# ---------------------------------------------------------------------------
# 子进程作业（后台线程；每次调用独立 DB 会话与临时工作目录）
# ---------------------------------------------------------------------------


def _run_mapping_job(case_id: int, token: str) -> None:
    """后台线程：注入 → 子进程 → 轮询 result.json → 对账落库/落失败态。"""
    workdir = Path(tempfile.mkdtemp(prefix="wts-mapping-"))
    db = SessionLocal()
    stdout_f = stderr_f = None
    try:
        case = db.get(TextCase, case_id)
        if case is None:
            return
        job = case.mapping_job
        if job is None or job.get("job_token") != token:
            return
        _inject_workdir(db, case, workdir)

        stdout_f = open(workdir / "cli.stdout.log", "w", encoding="utf-8")
        stderr_f = open(workdir / "cli.stderr.log", "w", encoding="utf-8")
        try:
            # start_new_session：CLI 自成进程组 leader，回收时连同 agent 孙进程
            proc = subprocess.Popen(
                glm_cli.build_command(
                    settings.glm_cli_path, settings.glm_mapping_skill, workdir
                ),
                cwd=workdir,
                stdout=stdout_f,
                stderr=stderr_f,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            _mark_failed(db, case, "cli_failed", f"无法启动 GLM CLI：{exc}")
            return
        with _trigger_lock:
            _active_procs[token] = proc

        try:
            parsed = glm_cli.await_result_file(
                proc,
                workdir / "result.json",
                settings.glm_timeout_seconds,
                MappingCLIResult,
            )
        except CLIJobError as exc:
            detail = exc.detail
            if exc.code == "cli_failed":
                stderr_f.flush()
                captured = (workdir / "cli.stderr.log").read_text(encoding="utf-8").strip()
                if captured:
                    detail = f"{detail}：{captured[:800]}"
            _mark_failed(db, case, exc.code, detail)
            return

        try:
            _persist_steps(db, case, parsed)
        except BadMappingResult as exc:
            _mark_failed(db, case, "bad_result", str(exc))
    except Exception as exc:
        # 未预期错误的即时兜底：不得让作业永久卡在 running（启动回收是第二道）。
        # 会话可能已处于失效状态，回滚后重取行并核对令牌再落失败。
        try:
            db.rollback()
            fresh = db.get(TextCase, case_id)
            if fresh is not None and (fresh.mapping_job or {}).get("job_token") == token:
                _mark_failed(db, fresh, "internal_error", f"映射作业内部错误：{exc}")
        except Exception:
            db.rollback()
    finally:
        with _trigger_lock:
            if _running_jobs.get(case_id) == token:
                del _running_jobs[case_id]
            _active_procs.pop(token, None)
        if stdout_f is not None:
            stdout_f.close()
        if stderr_f is not None:
            stderr_f.close()
        db.close()
        shutil.rmtree(workdir, ignore_errors=True)


def list_steps(db: Session, case_id: int) -> list[StructuredStep]:
    """查看结构化步骤（按 seq 升序）；未映射步骤由调用方按状态高亮。"""
    return list(
        db.execute(
            select(StructuredStep)
            .where(StructuredStep.text_case_id == case_id)
            .order_by(StructuredStep.seq)
        ).scalars().all()
    )


# ---------------------------------------------------------------------------
# 启动回收 / 关闭清理
# ---------------------------------------------------------------------------


def reap_interrupted_jobs(db: Session) -> int:
    """启动回收：DB 中残留 state=running 的映射作业统一落 failed/interrupted。

    首次映射中断时用例停在 elaborating、重新映射中断时停在 mapped——状态不
    足以识别在途作业，逐行查 mapping_job JSON。
    """
    reaped = 0
    for case in db.execute(select(TextCase)).scalars():
        job = case.mapping_job
        if job and job.get("state") == "running":
            job["state"] = "failed"
            job["error"] = {
                "code": "interrupted",
                "detail": "服务重启导致本次映射作业中断，请重新触发映射",
            }
            flag_modified(case, "mapping_job")
            reaped += 1
    if reaped:
        db.commit()
    return reaped


def shutdown_active_jobs() -> None:
    """服务关闭：整组回收全部在途映射 CLI，避免孤儿子进程。"""
    with _trigger_lock:
        procs = list(_active_procs.values())
        _active_procs.clear()
    for proc in procs:
        glm_cli.terminate_proc(proc)
