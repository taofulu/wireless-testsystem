"""沙盒内核（T9）：虚拟拓扑、仿真供给解释、逐步骤记录与三态判决。

体系位置（ADR-0009 / spec 模块划分 sandbox）：
- 后端按操作目录装配逐步骤仿真供给（simulatable/sim_ref/效果描述符），经
  sandbox-context 下发；本模块是执行侧解释器——加载三种仿真供给
  （schema_stub 自动桩 / declarative 效果描述符 / python 仿真包），在虚拟
  拓扑上执行生成的 pytest 代码，聚合 passed/failed/inconclusive 三态判决
- 仿真实现所有权归 AW 团队（随 catalog 交付）；本模块只解释供给，不手写
  任何操作的行为仿真
- 三态判定表与后端 app.sandbox.aggregate_verdict 一致（两侧独立实现同一
  规则表：worker 是不依赖后端的独立安装包）

运行时协议（pytest 子进程内）：
- 环境变量 WTS_SANDBOX_CONTEXT 指向 sandbox_context.json（后端下发的供给）
- 环境变量 WTS_SANDBOX_WORKDIR 指向任务临时目录（报告写在此处）
- 环境变量 WTS_SIM_PACKAGE_DIR 指向 AW 团队 Python 仿真包目录（可为空）
- 进程退出时（atexit）把逐步骤记录写入 sandbox_report.json
"""
import importlib
import json
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# 虚拟拓扑（故事 39：按所需拓扑自动实例化为标准默认态）
# ---------------------------------------------------------------------------

# 标准默认态：四类设备的初始状态树。调试预设（preset）以深合并方式覆盖。
DEFAULT_TOPOLOGY: dict[str, Any] = {
    "bbu": {"status": "commissioned", "cells": {}},
    "ue": {"registered": False},
    "instrument": {"connected": True, "rf_power_dbm": None, "playing": None},
    "mbb": {"reachable": True},
}


def deep_merge(base: dict, override: dict) -> dict:
    """深合并：override 的 dict 值递归覆盖，其余类型（含 list）整体替换。"""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def build_initial_state(preset: Optional[dict]) -> dict:
    """标准默认态 + 调试预设 → 本次调试会话的虚拟设备初始状态。"""
    if not preset:
        return json.loads(json.dumps(DEFAULT_TOPOLOGY))  # 深拷贝，防污染常量
    return deep_merge(json.loads(json.dumps(DEFAULT_TOPOLOGY)), preset)


# ---------------------------------------------------------------------------
# 纯函数：状态路径访问与模板替换（效果描述符解释器）
# ---------------------------------------------------------------------------


def get_path(tree: dict, path: str) -> Any:
    """按点分路径读状态树；路径不存在抛 KeyError（描述符引用错路径是供给 bug）。"""
    node: Any = tree
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(path)
        node = node[part]
    return node


def set_path(tree: dict, path: str, value: Any) -> None:
    """按点分路径写状态树；中间节点缺失时按 dict 创建。"""
    node = tree
    parts = path.split(".")
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


_PARAM_REF = re.compile(r"<([A-Za-z_][A-Za-z0-9_]*)>")
_STATE_REF = "@"


def _replace_param_refs(value: str, params: dict) -> str:
    """把字符串中所有 ``<name>`` 片段替换为对应参数（str 化）。"""
    def _sub(match: re.Match) -> str:
        key = match.group(1)
        if key not in params:
            raise KeyError(f"描述符引用未提供的参数 <{key}>")
        return str(params[key])
    return _PARAM_REF.sub(_sub, value)


def render_template(value: Any, params: dict, state: dict) -> Any:
    """描述符模板替换（递归）：

    - 字符串中 ``<name>`` 片段替换为调用参数（嵌在路径片段里用，如
      ``bbu.cell_<cell_id>.admin_state``）
    - 整个字符串恰为 ``<name>`` 时替换为参数原值（保留类型）
    - 整个字符串以 ``@`` 开头时按状态路径读回（有状态仿真的"设置后可读回"）；
      路径中的 ``<name>`` 占位符先替换再查询
    - dict/list 递归处理
    """
    if isinstance(value, str):
        if value.startswith(_STATE_REF):
            return get_path(state, _replace_param_refs(value[1:], params))
        whole = _PARAM_REF.fullmatch(value)
        if whole:
            key = whole.group(1)
            if key not in params:
                raise KeyError(f"描述符引用未提供的参数 <{key}>")
            return params[key]
        return _replace_param_refs(value, params)
    if isinstance(value, dict):
        return {k: render_template(v, params, state) for k, v in value.items()}
    if isinstance(value, list):
        return [render_template(item, params, state) for item in value]
    return value


def apply_descriptor(state: dict, descriptor: dict, params: dict) -> dict:
    """执行一个 declarative 效果描述符：先写状态路径，再渲染返回值。

    描述符 schema（ADR-0009；fault 字段位置预留，MVP 不启用失败注入）：
        {
          "writes":  [{"path": "bbu.cell_<cell_id>.admin_state", "value": "active"}],
          "returns": {"ok": true, "cell_id": "<cell_id>"}
        }
    """
    for write in descriptor.get("writes") or []:
        path = render_template(write["path"], params, state)
        value = render_template(write.get("value"), params, state)
        set_path(state, path, value)
    returns = descriptor.get("returns") or {}
    result = render_template(returns, params, state)
    if not isinstance(result, dict):
        raise ValueError("描述符 returns 必须渲染为对象")
    result.setdefault("ok", True)
    return result


# ---------------------------------------------------------------------------
# 纯函数：schema_stub 签名校验（params_schema 的最小 JSON Schema 子集）
# ---------------------------------------------------------------------------

_SCHEMA_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
}


def validate_stub_params(params_schema: dict, params: dict) -> Optional[str]:
    """L1 桩校验：必填字段存在 + 已声明属性的类型匹配；失败返回原因。

    未在 properties 中声明的额外参数按 JSON Schema 默认语义放行。
    """
    for name in params_schema.get("required") or []:
        if name not in params:
            return f"缺少必填参数 {name}"
    properties = params_schema.get("properties") or {}
    for name, value in params.items():
        spec = properties.get(name)
        if not isinstance(spec, dict):
            continue
        expected = _SCHEMA_TYPES.get(spec.get("type"))
        if expected is None:
            continue
        if isinstance(value, bool) and expected is not bool:
            return f"参数 {name} 类型应为 {spec['type']}"
        if not isinstance(value, expected):
            return f"参数 {name} 类型应为 {spec['type']}"
    return None


# ---------------------------------------------------------------------------
# 纯函数：级别聚合与三态判决（与后端 app.sandbox 同一判定表）
# ---------------------------------------------------------------------------

_LEVEL_RANK = {"simulated": 2, "schema_stub": 1, "unsimulated": 0}


def worst_level(levels) -> str:
    """组合操作有效级别：取子操作最差者（空序列按 unsimulated）。"""
    levels = list(levels)
    if not levels:
        return "unsimulated"
    return min(levels, key=lambda lv: _LEVEL_RANK[lv])


def aggregate_verdict(step_results) -> str:
    """三态判决（ADR-0009）：

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


def merge_step_results(context_steps, records: dict) -> list:
    """上下文步骤 × 运行时记录 → 完整逐步骤标注。

    未产生记录的步骤标 not_run（如前面步骤失败后未执行到），sim_level 仍取
    上下文供给声明——未运行不改变该步的仿真覆盖事实。
    """
    merged = []
    for meta in context_steps:
        rec = records.get(str(meta["seq"])) or records.get(meta["seq"])
        if rec is None:
            merged.append(
                {
                    "seq": meta["seq"],
                    "op": meta["op_name"],
                    "sim_level": meta["simulatable"],
                    "status": "not_run",
                }
            )
        else:
            merged.append(rec)
    return merged


# ---------------------------------------------------------------------------
# 运行时：pytest 子进程内的调度与记录（由 allure/aw 沙盒包驱动）
# ---------------------------------------------------------------------------


class SandboxRuntime:
    """一次沙盒执行的运行时：虚拟设备状态、AW 调用分发、逐步骤记录。

    线程安全（allure step 与 AW 调用均在 pytest 主线程，锁为防御性兜底）。
    """

    def __init__(self, context: dict, workdir: Path, sim_package_dir: str = ""):
        self.context = context
        self.workdir = Path(workdir)
        self.sim_package_dir = sim_package_dir
        self.state = build_initial_state(context.get("preset"))
        self.records: dict[int, dict] = {}
        self.artifacts: list[dict] = []
        self._current = threading.local()
        self._lock = threading.RLock()
        # (seq, op_name) 与 op_name 双索引：优先按当前步骤精确匹配
        self._by_seq_op = {}
        self._by_op = {}
        for meta in context.get("steps") or []:
            self._by_seq_op[(meta["seq"], meta["op_name"])] = meta
            self._by_op.setdefault(meta["op_name"], meta)

    # -- 步骤边界（allure 沙盒包驱动） ------------------------------------

    def begin_step(self, title: str) -> None:
        match = re.match(r"\s*步骤(\d+)", title)
        self._current.seq = int(match.group(1)) if match else None

    def end_step(self, exc: Optional[BaseException]) -> None:
        seq = getattr(self._current, "seq", None)
        if seq is None:
            return
        with self._lock:
            rec = self.records.get(seq)
            if rec is None:
                # 步骤内没有 AW 调用（如纯断言步骤）：按供给级别记 pass
                meta = self._meta_for(seq, None)
                rec = {
                    "seq": seq,
                    "op": meta["op_name"] if meta else "(无 AW 调用)",
                    "sim_level": meta["simulatable"] if meta else "unsimulated",
                    "status": "pass",
                }
                self.records[seq] = rec
            if exc is not None and rec["status"] != "fail":
                rec["status"] = "fail"
                rec["detail"] = f"{type(exc).__name__}: {exc}"[:500]
        self._current.seq = None

    # -- AW 调用分发（aw 沙盒包驱动） -------------------------------------

    def _meta_for(self, seq: Optional[int], op_name: Optional[str]) -> Optional[dict]:
        if seq is not None and op_name is not None:
            meta = self._by_seq_op.get((seq, op_name))
            if meta is not None:
                return meta
        if op_name is not None:
            return self._by_op.get(op_name)
        return None

    def call(self, namespace: str, op_name: str, params: dict) -> dict:
        """分发一次 AW 操作调用到对应仿真供给，返回操作结果 dict。"""
        seq = getattr(self._current, "seq", None)
        with self._lock:
            meta = self._meta_for(seq, op_name)
            if meta is None:
                return self._fail_step(
                    seq,
                    op_name,
                    f"AW 调用 {namespace}.{op_name} 不在沙盒上下文供给内"
                    "（映射/渲染与操作目录不一致）",
                )
            level = meta["simulatable"]
            try:
                result = self._dispatch(meta, params)
            except Exception as exc:  # 供给执行出错即本步 AW 报错（failed）
                return self._fail_step(seq, op_name, f"{type(exc).__name__}: {exc}")

            rec = self.records.get(seq) if seq is not None else None
            if rec is None and seq is not None:
                rec = {
                    "seq": seq,
                    "op": op_name,
                    "sim_level": level,
                    "status": "pass",
                }
                self.records[seq] = rec
            if meta.get("produces_artifacts"):
                artifact = {
                    "name": f"{op_name}-artifact",
                    "kind": "log_package",
                    "uri": f"sandbox://{op_name}/artifact-placeholder",
                    "checksum": "sha256:placeholder",
                }
                self.artifacts.append(artifact)
            return result

    def _dispatch(self, meta: dict, params: dict) -> dict:
        level = meta["simulatable"]
        if level == "unsimulated":
            return {"ok": True}  # 未仿真：仅保持结构完整，结论不可判定
        if level == "schema_stub":
            error = validate_stub_params(meta.get("params_schema") or {}, params)
            if error:
                raise ValueError(f"桩校验失败: {error}")
            return {"ok": True}
        # simulated：composite 按子操作序列执行；declarative/python 按供给
        if meta.get("kind") == "composite":
            return self._run_composite(meta, params)
        if meta.get("descriptor") is not None:
            return apply_descriptor(self.state, meta["descriptor"], params)
        if meta.get("sim_ref"):
            return self._run_python_sim(meta["sim_ref"], params)
        raise ValueError("simulated 步骤缺少仿真供给（descriptor/sim_ref 均空）")

    def _run_composite(self, meta: dict, params: dict) -> dict:
        """组合操作：按声明的有序子操作序列执行（ADR-0010），全部 ok 则 ok。"""
        subs = meta.get("suboperations") or []
        if not subs:
            raise ValueError(f"组合操作 {meta['op_name']} 缺少子操作序列")
        last: dict = {"ok": True}
        for sub in subs:
            if sub["simulatable"] == "simulated" and sub.get("descriptor") is not None:
                last = apply_descriptor(self.state, sub["descriptor"], params)
            elif sub["simulatable"] == "simulated" and sub.get("sim_ref"):
                last = self._run_python_sim(sub["sim_ref"], params)
            else:
                # 子操作仅桩/未仿真：保持结构（级别聚合已在上下文标为最差者）
                last = {"ok": True}
            if not last.get("ok"):
                break  # 子操作失败即短路（与真实 AW 组合语义一致）
        return {"ok": bool(last.get("ok"))}

    def _run_python_sim(self, sim_ref: str, params: dict) -> dict:
        """加载 AW 团队 Python 仿真包的 module:function 并执行。"""
        if not self.sim_package_dir:
            raise RuntimeError("未配置 Python 仿真包目录（--sim-package-dir）")
        module_name, _, func_name = sim_ref.partition(":")
        if not module_name or not func_name:
            raise ValueError(f"python 仿真引用 {sim_ref!r} 需为 module:function 形状")
        if self.sim_package_dir not in sys.path:
            sys.path.insert(0, self.sim_package_dir)
        func = getattr(importlib.import_module(module_name), func_name)
        result = func(self.state, params)
        if not isinstance(result, dict):
            raise ValueError(f"python 仿真 {sim_ref} 返回值必须是 dict")
        result.setdefault("ok", True)
        return result

    def _fail_step(self, seq: Optional[int], op_name: str, detail: str) -> dict:
        if seq is not None:
            self.records[seq] = {
                "seq": seq,
                "op": op_name,
                "sim_level": (
                    self._meta_for(seq, op_name) or {}
                ).get("simulatable", "unsimulated"),
                "status": "fail",
                "detail": detail[:500],
            }
        return {"ok": False, "error": detail}

    # -- 长时操作原语（declarative 仿真为立即完成，ADR-0010） ---------------

    def wait_completion(self, _handle) -> None:
        return None

    def collect_artifacts(self, _handle) -> list:
        return list(self.artifacts)

    # -- 报告落盘（atexit） -------------------------------------------------

    def finalize(self) -> None:
        report = {
            "steps": [self.records[k] for k in sorted(self.records)],
            "artifacts": self.artifacts,
        }
        (self.workdir / "sandbox_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )


_RUNTIME: Optional[SandboxRuntime] = None


def runtime() -> SandboxRuntime:
    """子进程内惰性初始化的运行时单例（由 allure/aw 沙盒包共享）。"""
    global _RUNTIME
    if _RUNTIME is None:
        context = json.loads(
            Path(os.environ["WTS_SANDBOX_CONTEXT"]).read_text(encoding="utf-8")
        )
        _RUNTIME = SandboxRuntime(
            context,
            Path(os.environ["WTS_SANDBOX_WORKDIR"]),
            os.environ.get("WTS_SIM_PACKAGE_DIR", ""),
        )
        import atexit

        atexit.register(_RUNTIME.finalize)
    return _RUNTIME


# ---------------------------------------------------------------------------
# 沙盒包（allure/aw 薄壳）：写入 pytest 子进程 PYTHONPATH 顶部
# ---------------------------------------------------------------------------

ALLURE_SANDBOX = '''"""allure 沙盒包（WTS 沙盒内核）：step 边界驱动运行时逐步骤记录。"""

from contextlib import contextmanager

from wts_worker.sandbox import runtime


@contextmanager
def step(title):
    rt = runtime()
    rt.begin_step(title)
    try:
        yield
    except BaseException as exc:
        rt.end_step(exc)
        raise
    else:
        rt.end_step(None)


def _identity_decorator(_name):
    def decorate(fn):
        return fn

    return decorate


def parent_suite(name):
    return _identity_decorator(name)


def suite(name):
    return _identity_decorator(name)
'''

AW_SANDBOX = '''"""AW 沙盒包（WTS 沙盒内核）：按沙盒上下文分发三种仿真供给。"""

from wts_worker.sandbox import runtime


class _Result:
    """操作返回值：dict 内容展平为属性（生成代码只断言 .ok）。"""

    def __init__(self, data):
        self._data = dict(data)
        for key, value in data.items():
            setattr(self, key, value)
        if not hasattr(self, "ok"):
            self.ok = True

    def __repr__(self):  # pragma: no cover - 调试便利
        return f"<aw.Result {self._data!r}>"


class _Namespace:
    """设备命名空间（aw.bbu / aw.ue / aw.instrument / aw.mbb）动态分发。"""

    def __init__(self, name):
        self._name = name

    def __getattr__(self, op_name):
        def _invoke(**params):
            return _Result(runtime().call(self._name, op_name, params))

        return _invoke


def wait_completion(handle):
    return runtime().wait_completion(handle)


def collect_artifacts(handle):
    return runtime().collect_artifacts(handle)


def __getattr__(namespace_name):
    if namespace_name.startswith("__"):
        raise AttributeError(namespace_name)
    return _Namespace(namespace_name)
'''


def write_sandbox_packages(pkg_dir: Path) -> None:
    """把 allure/aw 沙盒包写入指定目录（由调用方加入 pytest 的 PYTHONPATH）。"""
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "allure.py").write_text(ALLURE_SANDBOX, encoding="utf-8")
    (pkg_dir / "aw.py").write_text(AW_SANDBOX, encoding="utf-8")
