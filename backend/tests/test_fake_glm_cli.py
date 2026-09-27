"""fake GLM CLI 真实进程守护（不 mock 子进程）。

沿用 prior art：ai-hero-cli cli.pilot.test.ts 的真实进程模式。
该夹具是后续全链路票（T4/T5）的系统边界接缝，先在 T1 锁死其文件契约。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "fake_glm_cli.py"


def _run(workdir: Path, skill_name: str) -> subprocess.CompletedProcess:
    script = str(FIXTURE)
    cmd = [script, "--skill", skill_name, "--workdir", str(workdir)]
    # 以可执行脚本方式直接运行（守护 shebang 与可执行位），失败时回退解释器
    if os.access(script, os.X_OK):
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return subprocess.run([sys.executable, *cmd], capture_output=True, text=True, timeout=30)


def _prepare_workdir(tmp_path: Path, schema: dict) -> Path:
    wd = tmp_path / "glm-job"
    wd.mkdir()
    (wd / "input.md").write_text("# 用例文本\n", encoding="utf-8")
    (wd / "catalog_subset.json").write_text("[]", encoding="utf-8")
    (wd / "terms.json").write_text("[]", encoding="utf-8")
    (wd / "output_schema.json").write_text(json.dumps(schema), encoding="utf-8")
    return wd


def test_elaboration_mode_writes_sufficiency_result(tmp_path):
    wd = _prepare_workdir(tmp_path, {"properties": {"sufficient": {}, "missing_points": {}}})
    proc = _run(wd, "elaboration-skill")
    assert proc.returncode == 0, proc.stderr

    result = json.loads((wd / "result.json").read_text(encoding="utf-8"))
    assert result["sufficient"] is False
    point = result["missing_points"][0]
    assert set(point.keys()) == {"field", "question"}


def test_mapping_mode_writes_steps_result(tmp_path):
    wd = _prepare_workdir(tmp_path, {"properties": {"steps": {}}})
    proc = _run(wd, "mapping-skill")
    assert proc.returncode == 0, proc.stderr

    result = json.loads((wd / "result.json").read_text(encoding="utf-8"))
    step = result["steps"][0]
    assert set(step.keys()) == {
        "seq", "action_text", "aw_operation_id", "params", "assertion_text", "mapping_status"
    }
    assert step["mapping_status"] == "unmapped"


def test_missing_input_md_fails(tmp_path):
    wd = tmp_path / "bad-job"
    wd.mkdir()
    proc = _run(wd, "mapping-skill")
    assert proc.returncode != 0
    assert not (wd / "result.json").exists()


def test_missing_injection_file_fails(tmp_path):
    wd = tmp_path / "glm-job"
    wd.mkdir()
    (wd / "input.md").write_text("# 用例文本\n", encoding="utf-8")
    # 故意只放两个 JSON，缺 catalog_subset.json
    (wd / "terms.json").write_text("[]", encoding="utf-8")
    (wd / "output_schema.json").write_text('{"properties": {"steps": {}}}', encoding="utf-8")
    proc = _run(wd, "mapping-skill")
    assert proc.returncode != 0
    assert "catalog_subset.json" in proc.stderr
    assert not (wd / "result.json").exists()


def test_malformed_injection_json_fails(tmp_path):
    wd = _prepare_workdir(tmp_path, {"properties": {"steps": {}}})
    (wd / "terms.json").write_text("{ 坏 json", encoding="utf-8")
    proc = _run(wd, "mapping-skill")
    assert proc.returncode != 0
    assert "terms.json" in proc.stderr
