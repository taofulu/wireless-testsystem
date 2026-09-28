"""任务执行器：拉取的可执行用例代码 → 临时落盘 → 真实 pytest 子进程（T8）；
T9 加入沙盒内核执行通路（三态判决与逐步骤仿真标注）；T12 real 通路接入
testbed 侧 AW 包（--aw-package-dir），替换 T8 的 L1 桩包占位。

约束（Issue #9/#13 验收）：
- 真实 pytest 子进程 + testbed AW 包，不 mock 执行器
- 临时工作目录执行后清理（本地无需持久存储，故事 30）
- 超时/崩溃兜底为 failed 判决回传，Worker 自身不随任务崩溃

T9 沙盒通路（run_sandbox_case）：加载后端下发的沙盒上下文（三种仿真供给
+ 调试预设），以沙盒 allure/aw 包驱动逐步骤记录，聚合 passed/failed/
inconclusive 三态判决（ADR-0009：存在仅桩校验或未仿真步骤即不可判定，
禁止假绿）。

T12 real 通路（run_real_case）Worker ↔ testbed 报告协议：子进程环境注入
WTS_REAL_WORKDIR（任务临时目录）与 WTS_SERVER_URL（后端基地址，场景文件
无直通时的拉取降级通道，故事 53）；testbed 侧（真实 AW/allure 或伪夹具）
把 ``real_report.json`` 写入 WTS_REAL_WORKDIR::

    {"allure_report": {...} | null,
     "artifacts": [{"name", "kind", "uri", "checksum"}],
     "env_error": {"code": ..., "detail": ...} | null}

判决规则：env_error 非空 → env_failed（环境类失败与断言失败区分，故事 53）；
否则 pytest 退出码 0 → passed、非 0 → failed。报告缺失/损坏按无报告处理
（子进程早夭不退回假绿）。
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

# 单条任务日志回传上限（尾保留）：防止超长输出打爆请求体
MAX_LOG_CHARS = 100_000

# testbed 侧报告文件名（Worker ↔ testbed 报告协议，见模块 docstring）
REAL_REPORT_NAME = "real_report.json"


@dataclass
class TaskOutcome:
    """一次任务执行的结论；verdict 取值与后端 TaskResultIn 契约一致。"""

    verdict: str  # "passed" | "failed" | "inconclusive" | "env_failed"
    logs: str
    step_results: List[Any] = field(default_factory=list)
    artifacts: List[Any] = field(default_factory=list)
    allure_report: Optional[dict] = None


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


def run_real_case(
    code: str,
    work_root: Path,
    timeout_seconds: float = 600.0,
    aw_package_dir: str = "",
    server_url: str = "",
) -> TaskOutcome:
    """T12 real 通路：真实 pytest 子进程 + testbed 侧 AW 包执行（故事 30-33、53、54）。

    目录布局：``work_root/wts-real-XXXX/{test_case.py, real_report.json}``；
    子进程 PYTHONPATH 注入 testbed AW 包目录（AW 团队供给的真实 testbed 机
    上为真包，测试为伪夹具），环境注入 WTS_REAL_WORKDIR / WTS_SERVER_URL
    （报告协议见模块 docstring）；任何路径下结束都清理整个临时目录。
    """
    if not aw_package_dir:
        # real 通路没有 AW 包无从执行：诚实失败，不静默退回桩包（不假绿）
        return TaskOutcome(
            verdict="failed",
            logs="未配置 testbed AW 包目录（wts-worker run --aw-package-dir），拒绝执行 real 任务",
        )
    work_root = Path(work_root)
    work_root.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="wts-real-", dir=work_root))
    try:
        _write_pytest_case(workdir, code)

        env = dict(os.environ)
        env["WTS_REAL_WORKDIR"] = str(workdir)
        if server_url:
            env["WTS_SERVER_URL"] = server_url
        env = _with_pythonpath(env, [aw_package_dir])

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

        # 读 testbed 侧报告（Allure 结果、制品、环境类失败标记）；报告缺失/
        # 损坏（子进程早夭）按无报告处理——不改变 pytest 退出码的判决证据
        report: dict = {}
        report_path = workdir / REAL_REPORT_NAME
        if report_path.exists():
            try:
                parsed = json.loads(report_path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    report = parsed
            except json.JSONDecodeError:
                report = {}

        env_error = report.get("env_error")
        if env_error:
            # 环境类失败（场景文件不可达/无权限等）：与断言失败在判决上区分
            verdict = "env_failed"
            logs = _tail(
                f"环境类失败 [{env_error.get('code', '?')}]: "
                f"{env_error.get('detail', '')}\n{logs}"
            )
        else:
            verdict = "passed" if proc.returncode == 0 else "failed"
        artifacts = report.get("artifacts") or []
        if not isinstance(artifacts, list):
            artifacts = []
        allure_report = report.get("allure_report")
        if not isinstance(allure_report, dict):
            allure_report = None
        return TaskOutcome(
            verdict=verdict,
            logs=logs,
            artifacts=artifacts,
            allure_report=allure_report,
        )
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
