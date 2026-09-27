"""桩 AW 包与桩 allure 包（T8 沙盒通路）。

沙盒 Worker 执行生成的 pytest 代码时，真实 AW 库与 allure 在工程师本机
不可用；按 ADR-0009 的 L1 桩校验级提供最小可导入桩：

- allure 桩：``step`` 上下文管理器与 ``parent_suite``/``suite`` 装饰器，
  与生成代码（rendering 模块）的用法一一对应
- aw 桩：动态命名空间——``aw.<device>.<op>(**params)`` 任意调用返回
  ``ok=True`` 的结果对象；``wait_completion``/``collect_artifacts`` 为
  长时操作原语的占位

L1 桩只保证"代码可导入、可执行、断言结构完整"，不做任何行为仿真；
schema 驱动的签名校验与有状态仿真在 T9 内核替换本桩（本模块是执行通路
的占位接缝，仿真实现所有权归 AW 团队）。
"""
from pathlib import Path

ALLURE_STUB = '''"""allure 桩（WTS 沙盒 L1）：step 上下文与 suite 装饰器为无操作实现。"""

from contextlib import contextmanager


@contextmanager
def step(title):
    yield


def _identity_decorator(_name):
    def decorate(fn):
        return fn

    return decorate


def parent_suite(name):
    return _identity_decorator(name)


def suite(name):
    return _identity_decorator(name)
'''

AW_STUB = '''"""AW 桩（WTS 沙盒 L1）：任意设备命名空间下的任意操作返回 ok=True 结果。

仅支撑执行通路（拉代码→跑 pytest→回传）的端到端验证；不校验签名、
不仿真行为——逐步骤仿真级别与三态判决由 T9 沙盒内核接管。
"""


class _Result:
    """桩操作返回值：断言结构需要的最小形状。"""

    ok = True


def _stub_call(*args, **kwargs):
    return _Result()


class _Namespace:
    """设备命名空间（aw.bbu / aw.ue / aw.instrument / aw.mbb）动态分发。"""

    def __getattr__(self, _op_name):
        return _stub_call


def wait_completion(_handle):
    return None


def collect_artifacts(_handle):
    return []


def __getattr__(namespace_name):
    if namespace_name.startswith("__"):
        raise AttributeError(namespace_name)
    return _Namespace()
'''


def write_stub_packages(stubs_dir: Path) -> None:
    """把桩包写入指定目录（由调用方加入 pytest 子进程的 PYTHONPATH）。"""
    stubs_dir.mkdir(parents=True, exist_ok=True)
    (stubs_dir / "allure.py").write_text(ALLURE_STUB, encoding="utf-8")
    (stubs_dir / "aw.py").write_text(AW_STUB, encoding="utf-8")
