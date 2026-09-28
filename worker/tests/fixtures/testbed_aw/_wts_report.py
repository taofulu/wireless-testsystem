"""伪 testbed 报告读写助手（T12 夹具，allure/aw 伪包共用）。

Worker ↔ testbed 报告协议（wts_worker.runner 模块 docstring）：testbed 侧把
``real_report.json`` 写入 WTS_REAL_WORKDIR。本模块提供读-改-写原语；
伪 allure 在执行结束时写 allure_report，伪 aw 在执行中写 artifacts/env_error
（同进程顺序执行，无并发）。
"""
import json
import os
from pathlib import Path


def update(**fields) -> None:
    """把给定键合并进 real_report.json（值为 None 的键忽略）。"""
    workdir = os.environ.get("WTS_REAL_WORKDIR")
    if not workdir:
        return
    path = Path(workdir) / "real_report.json"
    report = {}
    if path.exists():
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            report = {}
    report.update({k: v for k, v in fields.items() if v is not None})
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
