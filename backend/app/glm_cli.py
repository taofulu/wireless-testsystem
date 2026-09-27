"""GLM CLI 子进程共享机制（ADR-0006）。

扩写（elaboration）与映射（mapping）以同一文件契约调用本地 GLM 5.2 CLI：
    <cli> --skill <skill_path> --workdir <dir>
CLI 内部自主完成 agent 循环并向工作目录写 result.json；后端轮询文件存在性
取结果、到点超时、退出时整组回收（连同 CLI 派生的 agent 孙进程）。

本模块只提供与业务无关的进程/轮询原语，作业状态机与注入文件由各业务模块
自持。
"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Type, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class CLIJobError(Exception):
    """子进程作业失败；code 落作业 JSON 的 error.code。"""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def build_command(cli_path: str, skill: str, workdir: Path) -> list[str]:
    """组装 CLI 命令行。

    无执行位的 .py 夹具/包装脚本以当前解释器运行；真实 glm-cli 直接 exec。
    """
    args = [cli_path, "--skill", skill, "--workdir", str(workdir)]
    if cli_path.endswith(".py"):
        return [sys.executable, *args]
    return args


def terminate_proc(proc: subprocess.Popen) -> None:
    """回收 CLI：新会话/进程组 leader 时整组杀掉（连同 CLI 的 agent 孙进程）。

    先 SIGTERM 再 SIGKILL；并发双回收（作业线程 + shutdown）安全幂等。
    """
    if proc.poll() is not None:
        return

    def _send(sig: int) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            try:
                proc.send_signal(sig)
            except ProcessLookupError:
                pass

    _send(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _send(signal.SIGKILL)
        proc.wait()


def await_result_file(
    proc: subprocess.Popen,
    result_path: Path,
    timeout: float,
    result_model: Type[T],
) -> T:
    """轮询 result.json 存在性直至完成；到点不产出即超时。

    result.json 一旦可按边界模型解析即视为作业完成——CLI 写完结果仍挂起时
    不再死等进程退出，而是回收进程后直接消费结果。文件写了一半（JSON 不成
    形/ schema 不符）时，进程存活且未超时则继续轮询。
    """
    deadline = time.monotonic() + timeout
    while True:
        if result_path.exists():
            try:
                import json

                raw = json.loads(result_path.read_text(encoding="utf-8"))
                parsed = result_model.model_validate(raw)
            except (json.JSONDecodeError, ValidationError):
                if proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                    continue
                if proc.poll() is None:
                    terminate_proc(proc)
                    raise CLIJobError(
                        "timeout", f"GLM CLI {timeout}s 内未产出合法 result.json"
                    )
            else:
                if proc.poll() is None:
                    terminate_proc(proc)
                return parsed

        returncode = proc.poll()
        if returncode is not None:
            break
        if time.monotonic() >= deadline:
            terminate_proc(proc)
            raise CLIJobError("timeout", f"GLM CLI 超过 {timeout}s 未产出 result.json")
        time.sleep(0.05)

    if returncode != 0:
        raise CLIJobError("cli_failed", f"GLM CLI 非零退出（{returncode}）")
    raise CLIJobError("bad_result", "CLI 已退出但 result.json 缺失或不符合输出 schema")
