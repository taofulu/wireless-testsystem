"""任务执行器：拉取的可执行用例代码 → 临时落盘 → 真实 pytest 子进程（T8）。

约束（Issue #9 验收）：
- 真实 pytest 子进程 + 桩 AW 包，不 mock 执行器
- 临时工作目录执行后清理（本地无需持久存储，故事 30）
- 超时/崩溃兜底为 failed 判决回传，Worker 自身不随任务崩溃

步骤级报告（Allure 解析、逐步骤结果）在 T9/T13 接入；T8 的 step_results
恒为空列表，判决只有 passed/failed（三态中的 inconclusive 由 T9 内核产出）。
"""
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List

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
        (workdir / "test_case.py").write_text(code, encoding="utf-8")

        env = os.environ.copy()
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (
            str(stubs_dir) + os.pathsep + existing if existing else str(stubs_dir)
        )
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
