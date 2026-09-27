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
"""
import argparse
import json
import sys
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
    result = ELABORATION_RESULT if mode == "elaboration" else MAPPING_RESULT
    (workdir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
