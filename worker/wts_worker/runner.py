"""任务执行器：拉取的可执行用例代码 → 临时落盘 → 真实 pytest 子进程（T8）；
T9 加入沙盒内核执行通路（三态判决与逐步骤仿真标注）。

约束（Issue #9 验收）：
- 真实 pytest 子进程 + 桩 AW 包，不 mock 执行器
- 临时工作目录执行后清理（本地无需持久存储，故事 30）
- 超时/崩溃兜底为 failed 判决回传，Worker 自身不随任务崩溃

T9 沙盒通路（run_sandbox_case）：加载后端下发的沙盒上下文（三种仿真供给
+ 调试预设），以沙盒 allure/aw 包驱动逐步骤记录，聚合 passed/failed/
inconclusive 三态判决（ADR-0009：存在仅桩校验或未仿真步骤即不可判定，
禁止假绿）。real 通路仍走 T8 桩包，真实 AW 接入在 T12。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

from wts_worker.sandbox import (
    aggregate_verdict,
    merge_step_results,
    write_sandbox_packages,
)
from wts_worker.stubs import write_stub_packages

# 单条任务日志回传上限（尾保留）：防止超长输出打爆请求体
MAX_LOG_CHARS = 100_000


@dataclass
class TaskOutcome:
    """一次任务执行的结论；verdict 取值与后端 TaskResultIn 契约一致。"""

    verdict: str  # "passed" | "failed"
    logs: str
    step_results: List[Any] = field(default_factory=list)
    artifacts: List[Any] = field(default_factory=list)


def _tail(text: str) -> str:
    if len(text) <= MAX_LOG_CHARS:
        return text
    return "……（前段截断）……\n" + text[-MAX_LOG_CHARS:]


def _write_pytest_case(workdir: Path, code: str) -> None:
    (workdir / "test_case.py").write_text(code, encoding="utf-8")


def _with_pythonpath(env: dict, paths: list[str]) -> dict:
    existing = env.get("PYTHONPATH")
    env = dict(env)
    env["PYTHONPATH"] = os.pathsep.join(
        list(paths) + ([existing] if existing else [])
    )
    return env


def run_pytest_case(
    code: str, work_root: Path, timeout_seconds: float = 600.0
) -> TaskOutcome:
    """在独立临时目录中执行一个可执行用例版本，返回判决与日志。

    目录布局：``work_root/wts-task-XXXX/{test_case.py, stubs/{allure,aw}.py}``；
    子进程以 PYTHONPATH=stubs 运行 ``python -m pytest``，任何路径下结束都
    清理整个临时目录。
    """
    work_root = Path(work_root)
    work_root.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="wts-task-", dir=work_root))
    try:
        stubs_dir = workdir / "stubs"
        write_stub_packages(stubs_dir)
        _write_pytest_case(workdir, code)

        env = _with_pythonpath(os.environ.copy(), [str(stubs_dir)])
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "test_case.py",
                ],
                cwd=workdir,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            partial = (exc.stdout or "") + (exc.stderr or "")
            return TaskOutcome(
                verdict="failed",
                logs=_tail(f"pytest 执行超时（{timeout_seconds}s），进程已终止\n{partial}"),
            )
        logs = _tail(proc.stdout + proc.stderr)
        # pytest 退出码：0 全过；1 有用例失败；2/3/4/5 收集/用法/无测试等
        # T8 通路判决二值：非 0 一律 failed（错误细分在 T9/T13）
        verdict = "passed" if proc.returncode == 0 else "failed"
        return TaskOutcome(verdict=verdict, logs=logs)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def run_sandbox_case(
    code: str,
    context: dict,
    work_root: Path,
    timeout_seconds: float = 600.0,
    sim_package_dir: str = "",
) -> TaskOutcome:
    """T9 沙盒通路：加载后端下发的沙盒上下文，以仿真 allure/aw 包驱动逐步骤记录。

    目录布局：``work_root/wts-sandbox-XXXX/{test_case.py, sandbox/{allure,aw}.py,
    sandbox_context.json, sandbox_report.json}``。

    进程内协议（与 wts_worker.sandbox.SandboxRuntime 约定）：
    - WTS_SANDBOX_CONTEXT: sandbox_context.json 路径
    - WTS_SANDBOX_WORKDIR: 任务临时目录（报告落盘处）
    - WTS_SIM_PACKAGE_DIR: AW 团队 Python 仿真包目录（可选）
    """
    work_root = Path(work_root)
    work_root.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="wts-sandbox-", dir=work_root))
    try:
        pkg_dir = workdir / "sandbox"
        write_sandbox_packages(pkg_dir)
        _write_pytest_case(workdir, code)

        ctx_path = workdir / "sandbox_context.json"
        ctx_path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")

        env = dict(os.environ)
        env["WTS_SANDBOX_CONTEXT"] = str(ctx_path)
        env["WTS_SANDBOX_WORKDIR"] = str(workdir)
        if sim_package_dir:
            env["WTS_SIM_PACKAGE_DIR"] = sim_package_dir
        # 以 wts_worker 自身（含 sandbox 模块）为 PYTHONPATH，使 pytest 子进程
        # 能 import wts_worker.sandbox；仿真空包（allure/aw）要置于其顶覆盖真实包
        wts_pkg = str(Path(__file__).resolve().parent.parent)
        env = _with_pythonpath(env, [str(pkg_dir), wts_pkg])

        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "test_case.py",
                ],
                cwd=workdir,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            partial = (exc.stdout or "") + (exc.stderr or "")
            return TaskOutcome(
                verdict="failed",
                logs=_tail(f"pytest 沙盒执行超时（{timeout_seconds}s），进程已终止\n{partial}"),
                step_results=[],
            )

        logs = _tail(proc.stdout + proc.stderr)
        # 读沙盒报告（逐步骤记录与三态判决）；报告缺失/损坏时（子进程早夭）
        # 按上下文供给级别合并 not_run 记录——缺报告不改变仿真覆盖事实，
        # not_run 在判决规则中按 failed 处理（缺执行证据，诚实优先）
        report_path = workdir / "sandbox_report.json"
        step_records: list = []
        artifacts: list = []
        if report_path.exists():
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
                step_records = report.get("steps", [])
                artifacts = report.get("artifacts", [])
            except json.JSONDecodeError:
                step_records = []
        records_by_seq = {
            rec.get("seq"): rec for rec in step_records if isinstance(rec, dict)
        }
        step_results = merge_step_results(context.get("steps") or [], records_by_seq)
        verdict = aggregate_verdict(step_results)
        return TaskOutcome(
            verdict=verdict, logs=logs, step_results=step_results, artifacts=artifacts
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
