"""伪 testbed allure 包（T12 夹具）：真实 allure 的系统边界替身。

与真包对齐的最小面：``step`` 上下文管理器（记录步骤标题与通过/失败）、
``parent_suite``/``suite`` 装饰器（无操作）。进程退出（atexit）时把步骤
记录合并进 WTS_REAL_WORKDIR/real_report.json 的 allure_report 键——对应
真实环境 allure-pytest 生成结果文件后由 Worker 收集的接缝。
"""
import atexit

import _wts_report

_steps = []


def _flush() -> None:
    _wts_report.update(allure_report={"steps": list(_steps)})


class step:
    """步骤上下文：正常退出记 passed，异常传播记 failed。"""

    def __init__(self, title):
        self._record = {"title": title, "status": "passed"}
        _steps.append(self._record)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc, _tb):
        if exc_type is not None:
            self._record["status"] = "failed"
        return False


def _identity_decorator(_name):
    def decorate(fn):
        return fn

    return decorate


def parent_suite(name):
    return _identity_decorator(name)


def suite(name):
    return _identity_decorator(name)


atexit.register(_flush)
