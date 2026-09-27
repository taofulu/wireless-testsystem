"""elaboration 模块：扩写问答闭环（T4；ADR-0006 子进程契约 / ADR-0007 扩写前置）。

职责（spec 模块划分）：
- 组装临时工作目录注入文件（input.md / terms.json / catalog_subset.json /
  output_schema.json），以子进程异步调用 GLM CLI（扩写 skill）
- 轮询状态由 DB 承担：作业状态写入 text_case.elaboration_qa，前端轮询 API
- sufficient=false 产出 missing_points；工程师逐条回答后由纯函数合并回三栏原文
- CLI 超时/非零退出/坏输出统一落 failed：不动原文、不写伪追问，可重试或跳过

状态接缝：扩写阶段不新增用例状态（spec 8 态固定）。用例处于 elaborating 时，
闸门状态存于 elaboration_qa.state；sufficient/skipped 为闸门通过，供 T5 的
POST /map 校验。
"""
import json
import shutil
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm.exc import StaleDataError

from app import glm_cli
from app.config import settings
from app.db import SessionLocal
from app.glm_cli import CLIJobError
from app.models import TextCase, TextCaseStatus
from app.schemas import ElaborationAnswersIn, ElaborationCLIResult

# 三栏字段（CONTEXT.md：文本用例固定由预知条件/测试步骤/预期结果三段组成）
TEXT_FIELDS = ("precondition", "steps_text", "expected_text")
FIELD_TITLES = {
    "precondition": "预知条件",
    "steps_text": "测试步骤",
    "expected_text": "预期结果",
}


def build_output_schema() -> dict[str, Any]:
    """注入给 CLI 的 output_schema.json（spec 168 行契约）。

    直接由后端边界校验模型 ElaborationCLIResult 导出，保证"要求 CLI 产出的
    形状"与"后端实际验收的形状"同源，不允许双份手写漂移。
    """
    schema = ElaborationCLIResult.model_json_schema()
    schema["$schema"] = "http://json-schema.org/draft-07/schema#"
    return schema


class ElaborationConflict(Exception):
    """当前扩写状态不允许该操作；路由统一转 409。"""


class StaleAnswers(Exception):
    """回答与当轮 missing_points 不一致（过期表单/漏答/空白）；路由转 422。"""


# ---------------------------------------------------------------------------
# 纯函数：注入文本渲染与答案合并（多轮迭代的核心，单测可直接覆盖）
# ---------------------------------------------------------------------------


def render_input_md(case: dict[str, Any]) -> str:
    """把三栏用例渲染成注入 CLI 的 input.md。"""
    lines = [f"# 用例：{case['title']}", ""]
    for field in TEXT_FIELDS:
        lines.append(f"## {FIELD_TITLES[field]}")
        content = (case[field] or "").strip()
        lines.append(content if content else "（未填写）")
        lines.append("")
    return "\n".join(lines)


def merge_answers(current: dict, answers: list[dict]) -> dict:
    """逐条回答合并回原文：答案以"补充"条目追加到所追问字段的栏目下。

    - 保留原文（新版本而非覆盖）
    - 回答附带来源追问，便于下一轮 CLI 评估理解补充语境
    - 同一字段多条回答生成多条补充；空字段不留前导空行
    """
    merged = {field: (current.get(field) or "") for field in TEXT_FIELDS}
    for item in answers:
        field = item["field"]
        question = item["question"].strip()
        answer = item["answer"].strip()
        line = f"- 补充（追问：{question}）：{answer}"
        base = merged[field].strip()
        merged[field] = f"{base}\n{line}" if base else line
    return merged


# ---------------------------------------------------------------------------
# 状态流转（由路由调用，每个函数一个事务边界）
# ---------------------------------------------------------------------------


def _commit_qa(db: Session, case: TextCase) -> bool:
    """提交扩写 JSON 变更，返回是否落库成功。

    elaboration_qa 为 JSON 列，原地修改（qa["state"] = ...）默认不被
    SQLAlchemy 变更跟踪，必须显式 flag_modified 才会发出 UPDATE。
    两道安静退出的守卫（防旧作业写脏新一轮数据）：
    - 用例行可能已不存在（如测试清表）→ 回滚返回 False
    - 行内 job_token 与本会话持有的不一致（行已属于更新的一次触发）→ 放弃
    """
    qa = case.elaboration_qa or {}
    token = qa.get("job_token")
    if token is not None:
        table = TextCase.__table__
        stmt = select(table.c.elaboration_qa).where(table.c.id == case.id)
        with db.no_autoflush:
            row = db.execute(stmt).first()
        if row is None or (row[0] or {}).get("job_token") != token:
            db.rollback()
            return False

    flag_modified(case, "elaboration_qa")
    try:
        db.commit()
    except StaleDataError:
        db.rollback()
        return False
    return True


def _new_qa(round_num: int, state: str, job_token: Optional[str] = None) -> dict:
    return {
        "state": state,
        "round": round_num,
        "missing_points": [],
        "rounds": [],
        "error": None,
        "job_token": job_token,
    }


# 进程内触发准入：FastAPI 同步端点跑在线程池，两个并发 POST 可能同时通过
# “检查→置 running”。MVP 为单 uvicorn 进程部署（Worker 是独立进程但不调用
# 本函数），进程锁足以消除竞态；若将来多进程部署，需改为 SELECT ... FOR UPDATE
# 行级条件更新。DB 中 state=running 仍是崩溃恢复判断的权威依据。
_trigger_lock = threading.Lock()
# case_id → 本次作业令牌。令牌键控（而非裸 id 集合）是为了让“上一轮作业
# 迟到的收尾”无法误删新一轮作业的登记；作业 finally 只在令牌仍匹配时摘除。
_running_jobs: dict[int, str] = {}
# 在途 CLI 子进程（令牌 → Popen）：服务 shutdown 时整组回收，不留孤儿。
_active_procs: dict[str, subprocess.Popen] = {}


def start_elaboration(db: Session, case: TextCase) -> None:
    """触发扩写：校验闸门 → 置 running → 起子进程线程（立即返回，可轮询）。"""
    if case.status not in (TextCaseStatus.DRAFT, TextCaseStatus.ELABORATING):
        raise ElaborationConflict("用例已越过扩写阶段，不能重新触发扩写")

    with _trigger_lock:
        if case.id in _running_jobs:
            raise ElaborationConflict("扩写评估正在进行中")

        token = uuid.uuid4().hex
        qa: Optional[dict] = case.elaboration_qa
        if qa is None:
            qa = _new_qa(round_num=1, state="running", job_token=token)
        elif qa["state"] == "running":
            raise ElaborationConflict("扩写评估正在进行中")
        elif qa["state"] == "awaiting_answers":
            raise ElaborationConflict("请先逐条回答本轮追问，或强制跳过扩写")
        elif qa["state"] in ("sufficient", "skipped"):
            raise ElaborationConflict("扩写已完成（已通过或已跳过），请进入映射")
        else:
            # answered：进入下一轮；failed：重试同一轮
            next_round = qa["round"] + 1 if qa["state"] == "answered" else qa["round"]
            history = qa["rounds"]
            qa = _new_qa(round_num=next_round, state="running", job_token=token)
            qa["rounds"] = history

        case.elaboration_qa = qa
        case.status = TextCaseStatus.ELABORATING
        db.commit()
        _running_jobs[case.id] = token

    thread = threading.Thread(
        target=_run_elaboration_job,
        args=(case.id, token),
        name=f"wts-elaboration-{case.id}",
        daemon=True,
    )
    thread.start()


def submit_answers(db: Session, case: TextCase, payload: ElaborationAnswersIn) -> None:
    """逐条回答合并回原文形成新版本，停在 answered 等待再次扩写或跳过。"""
    qa = case.elaboration_qa
    if qa is None or qa["state"] != "awaiting_answers":
        raise ElaborationConflict("当前没有等待回答的扩写追问")

    current_points = [(p["field"], p["question"]) for p in qa["missing_points"]]
    given = [(a.field, a.question) for a in payload.answers]
    if len(given) != len(set(given)):
        raise StaleAnswers("存在重复回答")
    if set(given) != set(current_points):
        raise StaleAnswers("回答必须逐条对应当前列出的全部追问")
    if any(not a.answer.strip() for a in payload.answers):
        raise StaleAnswers("回答不能为空白")

    merged = merge_answers(
        {field: getattr(case, field) for field in TEXT_FIELDS},
        [a.model_dump() for a in payload.answers],
    )
    for field in TEXT_FIELDS:
        setattr(case, field, merged[field])

    qa["state"] = "answered"
    qa["missing_points"] = []
    qa["rounds"][-1]["answers"] = [
        {"field": a.field, "question": a.question, "answer": a.answer.strip()}
        for a in payload.answers
    ]
    if not _commit_qa(db, case):
        raise ElaborationConflict("扩写状态已被更新的一次触发取代，请刷新后重试")


def skip_elaboration(db: Session, case: TextCase) -> None:
    """强制跳过扩写（故事 6）：不调用 CLI，闸门直接记为 skipped。"""
    if case.status not in (TextCaseStatus.DRAFT, TextCaseStatus.ELABORATING):
        raise ElaborationConflict("用例已越过扩写阶段，无需跳过")
    qa = case.elaboration_qa
    if qa is not None and qa["state"] == "running":
        raise ElaborationConflict("扩写评估正在进行中，请等待本次评估结束后再跳过")
    if qa is not None and qa["state"] in ("sufficient", "skipped"):
        raise ElaborationConflict("扩写已完成（已通过或已跳过），无需重复跳过")

    if qa is None:
        qa = _new_qa(round_num=0, state="skipped")
    else:
        qa["state"] = "skipped"
        qa["missing_points"] = []
        qa["error"] = None
    case.elaboration_qa = qa
    case.status = TextCaseStatus.ELABORATING
    if not _commit_qa(db, case):
        raise ElaborationConflict("扩写状态已被更新的一次触发取代，请刷新后重试")


# 进入扩写评估输入的字段（input.md 由标题与三栏构成）；其中任何一项的值发生
# 变化，既有的充分性结论即失效。
_GATE_INPUT_FIELDS = ("title",) + TEXT_FIELDS


def apply_text_case_patch(case: TextCase, updates: dict) -> None:
    """草稿/扩写态继续编辑：更新字段；评估输入真的发生变化时维护闸门：

    - running：注入已经发生，编辑会让输入与结论脱节 → 拒绝（409）
    - awaiting_answers：旧追问针对旧文本 → 作废本轮追问，回 answered 重评
    - sufficient/skipped：闸门结论失效 → 回 answered，须重新扩写或再次跳过
      （ADR-0007：不允许让未评估文本流入映射）
    仅"字段出现"不算变化，新值与现值相同（含空 PATCH）不动闸门。
    """
    qa = case.elaboration_qa
    if qa is not None and qa["state"] == "running":
        raise ElaborationConflict("扩写评估正在进行中，请等待结束后再编辑")

    # 映射作业进行中同样禁止改评估输入：注入文本与结论会脱节（函数内延迟
    # 导入，避免 elaboration ↔ mapping 模块级循环依赖）
    from app import mapping as mapping_module

    if mapping_module.is_mapping_running(case):
        raise ElaborationConflict("映射正在进行中，请等待结束后再编辑")

    changed_fields = [
        field
        for field in _GATE_INPUT_FIELDS
        if field in updates and updates[field] != getattr(case, field)
    ]
    for field, value in updates.items():
        setattr(case, field, value)

    if changed_fields and qa is not None and qa["state"] in (
        "awaiting_answers",
        "sufficient",
        "skipped",
    ):
        qa["state"] = "answered"
        qa["missing_points"] = []
        qa["error"] = None
        flag_modified(case, "elaboration_qa")


# ---------------------------------------------------------------------------
# 子进程作业（后台线程；每次调用独立 DB 会话与临时工作目录）
# ---------------------------------------------------------------------------


def _inject_workdir(case: TextCase, workdir: Path) -> None:
    """按 ADR-0006 契约写入注入文件。

    扩写评估只消费用例文本；术语库 repo 尚未 pin commit 供给（spec Further
    Notes），terms.json 先以空集占位；catalog_subset 对扩写无候选预筛需求，
    同为空集。两个文件仍是契约必选项，fake CLI 会校验其存在性。
    """
    case_view = {"title": case.title}
    case_view.update({field: getattr(case, field) for field in TEXT_FIELDS})
    (workdir / "input.md").write_text(render_input_md(case_view), encoding="utf-8")
    (workdir / "catalog_subset.json").write_text("[]", encoding="utf-8")
    (workdir / "terms.json").write_text("[]", encoding="utf-8")
    (workdir / "output_schema.json").write_text(
        json.dumps(build_output_schema(), ensure_ascii=False), encoding="utf-8"
    )


def _mark_failed(db: Session, case: TextCase, code: str, detail: str) -> bool:
    """落明确失败态：清空追问，绝不产生脏数据。返回是否落库成功。"""
    qa = case.elaboration_qa
    qa["state"] = "failed"
    qa["missing_points"] = []
    qa["error"] = {"code": code, "detail": detail[:1000]}
    return _commit_qa(db, case)


def _run_elaboration_job(case_id: int, token: str) -> None:
    """后台线程：注入 → 子进程 → 轮询 result.json → 回写作业状态。"""
    workdir = Path(tempfile.mkdtemp(prefix="wts-elaboration-"))
    db = SessionLocal()
    stdout_f = stderr_f = None
    try:
        case = db.get(TextCase, case_id)
        if case is None:  # 理论上不会发生（无删除用例 API），防御性退出
            return
        qa = case.elaboration_qa
        if qa is None or qa.get("job_token") != token:
            # 行已不属于本次触发（崩溃重启/测试复用 id 后的陈旧线程）：不起进程
            return
        _inject_workdir(case, workdir)

        stdout_f = open(workdir / "cli.stdout.log", "w", encoding="utf-8")
        stderr_f = open(workdir / "cli.stderr.log", "w", encoding="utf-8")
        try:
            # start_new_session：CLI 自成进程组 leader，回收时可连同其派生的
            # agent 孙进程一起 killpg（ADR-0006：CLI 内部自主跑 agent 循环）
            proc = subprocess.Popen(
                glm_cli.build_command(
                    settings.glm_cli_path, settings.glm_elaboration_skill, workdir
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
                ElaborationCLIResult,
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

        qa = case.elaboration_qa
        if parsed.sufficient:
            qa["state"] = "sufficient"
            qa["missing_points"] = []
            qa["error"] = None
            qa["rounds"].append({"round": qa["round"], "missing_points": [], "answers": []})
            _commit_qa(db, case)
            return

        points = [
            {"field": point.field, "question": point.question}
            for point in parsed.missing_points
        ]
        if not points:
            # sufficient=false 却给不出缺失点：自相矛盾的输出，按坏结果处理
            _mark_failed(db, case, "bad_result", "sufficient=false 但 missing_points 为空")
            return

        qa["state"] = "awaiting_answers"
        qa["missing_points"] = points
        qa["error"] = None
        qa["rounds"].append({"round": qa["round"], "missing_points": points, "answers": []})
        _commit_qa(db, case)
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


def reap_interrupted_jobs(db: Session) -> int:
    """启动回收：进程被杀后内存里的线程/进程登记全部消失，但 DB 里可能残留
    state=running 的作业——没有任何活线程会再回写它们。把这些行统一落
    failed/interrupted（允许重试或跳过），返回回收条数。

    DB state=running 因此真正承担"崩溃恢复权威依据"：恢复动作发生在启动时。
    """
    cases = db.execute(select(TextCase)).scalars()
    reaped = 0
    for case in cases:
        qa = case.elaboration_qa
        if case.status == TextCaseStatus.ELABORATING and qa and qa["state"] == "running":
            qa["state"] = "failed"
            qa["missing_points"] = []
            qa["error"] = {
                "code": "interrupted",
                "detail": "服务重启导致本次扩写作业中断，请重试或强制跳过",
            }
            flag_modified(case, "elaboration_qa")
            reaped += 1
    if reaped:
        db.commit()
    return reaped


def shutdown_active_jobs() -> None:
    """服务关闭：整组回收全部在途 CLI，避免 daemon 线程被杀后子进程孤儿化。"""
    with _trigger_lock:
        procs = list(_active_procs.values())
        _active_procs.clear()
    for proc in procs:
        glm_cli.terminate_proc(proc)
