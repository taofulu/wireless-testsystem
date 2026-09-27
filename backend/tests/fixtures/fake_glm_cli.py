#!/usr/bin/env python3
"""fake GLM CLI —— 系统边界夹具（ADR-0006）。

模拟本地 GLM 5.2 CLI 的文件契约：
    glm-cli --skill <skill_path> --workdir <dir>

读取工作目录中的 output_schema.json（与注入的其他文件），按 skill 模式
向工作目录写入固定的 result.json：
  - elaboration（扩写）：{sufficient, missing_points, elaborated_text?}
  - mapping（映射）：    {steps: [{seq, action_text, aw_operation_id?,
                                  params, assertion_text, mapping_status}]}

仅使用标准库，保证在未安装后端依赖的环境（如真实 Worker 机）也能执行。
真实进程测试见 test_fake_glm_cli.py。

扩写场景由环境变量 FAKE_GLM_ELABORATION 选择（模拟同一份用例在不同轮次/
质量下的评估结论），供 T4 全链路测试驱动状态分支：
    insufficient（默认） 充分性不足，产出 missing_points
    sufficient           充分，直接通过
    fail                 注入文件校验通过后进程失败（非零退出、不写 result.json）
    malformed            写出无法解析的 result.json
    slow                 长时间不退出、不写结果（配合后端超时验证 timeout 失败态）
    write_and_hang       写完 result.json 后进程挂起不退出（守护后端按文件
                         存在性轮询取结果，而非死等进程退出）

映射场景由 FAKE_GLM_MAPPING 选择，供 T5 驱动"字典命中/字典拦截"两类夹具。
映射夹具会真正阅读 catalog_subset.json：按操作名取后端注入的真实条目 ID
产出步骤，与真实 skill"只能从候选集里选操作"的行为一致：
    unmapped（默认） 全部步骤未映射
    matched          mml_family + mml_generic 各一步，通用 MML 命令字典命中
    intercepted      通用 MML 命令不在字典（unknown_command）→ 服务端须拦截
    bad_params       通用 MML 参数越界（out_of_range）→ 服务端须拦截
    unknown_op       自报 mapped 但引用候选集外 ID → 服务端须降级
    fail/malformed/slow/write_and_hang 与扩写同义
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ELABORATION_RESULT = {
    "sufficient": False,
    "missing_points": [
        {
            "field": "precondition",
            "question": "请明确 UE 是否已注册并附着到目标小区。",
        }
    ],
    "elaborated_text": None,
}

MAPPING_RESULT = {
    "steps": [
        {
            "seq": 1,
            "action_text": "激活目标小区",
            "aw_operation_id": None,
            "params": {},
            "assertion_text": "小区状态为激活",
            "mapping_status": "unmapped",
        }
    ]
}


def _detect_mode(skill_path: str, workdir: Path) -> str:
    """优先按 skill 名判断；缺省时按输出 schema 的特征键判断。"""
    skill_name = Path(skill_path).name.lower()
    if "elaborat" in skill_name:
        return "elaboration"
    if "map" in skill_name:
        return "mapping"

    schema_file = workdir / "output_schema.json"
    if schema_file.exists():
        schema = json.loads(schema_file.read_text(encoding="utf-8"))
        keys = set(schema.get("properties", schema).keys())
        if "sufficient" in keys or "missing_points" in keys:
            return "elaboration"
        if "steps" in keys:
            return "mapping"
    raise SystemExit("fake_glm_cli: 无法判定 skill 模式（skill 名无特征且缺少 output_schema.json）")


REQUIRED_INPUTS = ("input.md", "catalog_subset.json", "terms.json", "output_schema.json")


def _read_injection_files(workdir: Path) -> None:
    """按 ADR-0006 契约校验全部注入文件存在且 JSON 文件可解析。

    夹具守护的是后端的注入行为：缺文件或写坏 JSON 说明接线有问题，
    必须在测试期暴露，而不是静默产出 result.json。
    """
    for name in REQUIRED_INPUTS:
        path = workdir / name
        if not path.exists():
            print(f"fake_glm_cli: 缺少注入文件 {name}", file=sys.stderr)
            raise SystemExit(2)
        if name.endswith(".json"):
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                print(f"fake_glm_cli: {name} 不是合法 JSON: {exc}", file=sys.stderr)
                raise SystemExit(2)


ELABORATION_SUFFICIENT_RESULT = {
    "sufficient": True,
    "missing_points": [],
    "elaborated_text": None,
}


def _write_elaboration_result(workdir: Path) -> None:
    """按 FAKE_GLM_ELABORATION 产出扩写结论或模拟 CLI 故障。"""
    scenario = os.environ.get("FAKE_GLM_ELABORATION", "insufficient")
    if scenario == "slow":
        time.sleep(float(os.environ.get("FAKE_GLM_SLEEP_SECONDS", "30")))
        return
    if scenario == "fail":
        print("fake_glm_cli: 模拟 CLI 内部错误，扩写 agent 循环中断", file=sys.stderr)
        raise SystemExit(3)

    if scenario == "sufficient":
        result = ELABORATION_SUFFICIENT_RESULT
    elif scenario == "insufficient":
        result = ELABORATION_RESULT
    elif scenario == "write_and_hang":
        (workdir / "result.json").write_text(
            json.dumps(ELABORATION_SUFFICIENT_RESULT, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        time.sleep(float(os.environ.get("FAKE_GLM_SLEEP_SECONDS", "30")))
        return
    elif scenario == "malformed":
        (workdir / "result.json").write_text("{ 这不是合法 JSON", encoding="utf-8")
        return
    else:
        print(f"fake_glm_cli: 未知扩写场景 {scenario!r}", file=sys.stderr)
        raise SystemExit(2)

    (workdir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# 映射模式（T5）
# ---------------------------------------------------------------------------


def _validate_mapping_subset(workdir: Path) -> list:
    """守护映射注入契约：catalog_subset 条目必须携带 kind/device_target；

    mml_generic 条目必须挂命令字典片段（commands 列表；字典缺失时可为空）。
    契约被破坏说明后端预筛注入接线有问题，立即失败而非假装映射成功。
    """
    subset = json.loads((workdir / "catalog_subset.json").read_text(encoding="utf-8"))
    if not isinstance(subset, list):
        print("fake_glm_cli: catalog_subset.json 必须是 JSON 数组", file=sys.stderr)
        raise SystemExit(2)
    for entry in subset:
        if not isinstance(entry, dict):
            print("fake_glm_cli: catalog_subset 条目必须是对象", file=sys.stderr)
            raise SystemExit(2)
        for key in ("id", "name", "kind", "device_target"):
            value = entry.get(key)
            if not isinstance(value, (str, int)) or value == "":
                print(
                    f"fake_glm_cli: catalog_subset 条目缺少 {key}（候选须携带 kind/device_target）",
                    file=sys.stderr,
                )
                raise SystemExit(2)
        if entry["kind"] == "mml_generic" and not isinstance(entry.get("commands"), list):
            print(
                "fake_glm_cli: mml_generic 候选条目必须挂 commands 字典片段",
                file=sys.stderr,
            )
            raise SystemExit(2)
    return subset


def _mapped_step(seq, action, op_id, params, assertion):
    return {
        "seq": seq,
        "action_text": action,
        "aw_operation_id": op_id,
        "params": params,
        "assertion_text": assertion,
        "mapping_status": "mapped",
    }


def _build_mapping_payload(workdir: Path, scenario: str) -> dict:
    if scenario == "unmapped":
        return MAPPING_RESULT

    subset = _validate_mapping_subset(workdir)
    by_name = {entry["name"]: entry for entry in subset}

    if scenario in ("matched", "intercepted", "bad_params"):
        act_cell = by_name.get("act_cell")
        run_mml = by_name.get("run_mml")
        if act_cell is None or run_mml is None:
            print(
                "fake_glm_cli: 映射夹具需要 catalog_subset 含 act_cell 与 run_mml 候选",
                file=sys.stderr,
            )
            raise SystemExit(2)
        if scenario == "matched":
            # DSP_CELL(cell_id) 在演示字典内且参数合法：字典命中
            command, args = "DSP_CELL", {"cell_id": 1}
        elif scenario == "intercepted":
            # 命令不在字典：先于参数校验即判 unknown_command
            command, args = "LST_BOGUS", {}
        else:
            # 命令命中但 cell_id 越界：参数非法
            command, args = "DSP_CELL", {"cell_id": 999999}
        return {
            "steps": [
                _mapped_step(1, "激活目标小区", act_cell["id"], {"cell_id": 1}, "小区状态为激活"),
                _mapped_step(
                    2,
                    "动态查询小区状态（通用 MML）",
                    run_mml["id"],
                    {"command": command, "args": args},
                    "返回小区状态",
                ),
            ]
        }

    if scenario == "unknown_op":
        # 自报 mapped 却引用候选集之外的 ID，服务端必须降级 unmapped
        return {
            "steps": [
                _mapped_step(
                    1, "执行冷门操作", 999999, {}, "操作返回成功"
                )
            ]
        }

    print(f"fake_glm_cli: 未知映射场景 {scenario!r}", file=sys.stderr)
    raise SystemExit(2)


def _write_mapping_result(workdir: Path) -> None:
    """按 FAKE_GLM_MAPPING 产出结构化步骤或模拟 CLI 故障。"""
    scenario = os.environ.get("FAKE_GLM_MAPPING", "unmapped")

    if scenario == "slow":
        time.sleep(float(os.environ.get("FAKE_GLM_SLEEP_SECONDS", "30")))
        return
    if scenario == "fail":
        print("fake_glm_cli: 模拟 CLI 内部错误，映射 agent 循环中断", file=sys.stderr)
        raise SystemExit(3)
    if scenario == "malformed":
        (workdir / "result.json").write_text("{ 这不是合法 JSON", encoding="utf-8")
        return

    if scenario == "write_and_hang":
        # 挂起前写出一份合法结果（默认未映射负载），守护后端按文件存在性取结果
        payload = MAPPING_RESULT
        (workdir / "result.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        time.sleep(float(os.environ.get("FAKE_GLM_SLEEP_SECONDS", "30")))
        return

    # 正常场景的结果构造（同时完成注入契约校验）
    payload = _build_mapping_payload(workdir, scenario)

    (workdir / "result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="glm-cli")
    parser.add_argument("--skill", required=True)
    parser.add_argument("--workdir", required=True)
    args = parser.parse_args(argv)

    workdir = Path(args.workdir)
    if not workdir.is_dir():
        print(f"fake_glm_cli: 工作目录不存在: {workdir}", file=sys.stderr)
        return 2

    _read_injection_files(workdir)
    mode = _detect_mode(args.skill, workdir)
    if mode == "elaboration":
        _write_elaboration_result(workdir)
    else:
        _write_mapping_result(workdir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
