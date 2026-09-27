"""fake GLM CLI 真实进程守护（不 mock 子进程）。

沿用 prior art：ai-hero-cli cli.pilot.test.ts 的真实进程模式。
该夹具是后续全链路票（T4/T5）的系统边界接缝，先在 T1 锁死其文件契约。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

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


def _mapping_workdir(tmp_path: Path, catalog_subset: list) -> Path:
    wd = _prepare_workdir(tmp_path, {"properties": {"steps": {}}})
    (wd / "catalog_subset.json").write_text(
        json.dumps(catalog_subset), encoding="utf-8"
    )
    return wd


_DEMO_SUBSET = [
    {"id": 1, "name": "act_cell", "kind": "mml_family", "device_target": "bbu"},
    {
        "id": 2,
        "name": "run_mml",
        "kind": "mml_generic",
        "device_target": "bbu",
        "dictionary_ref": "bbu-mml-dict",
        "dictionary_version": "demo-bbu-v1.2",
        "commands": [{"command": "DSP_CELL", "params": [{"name": "cell_id", "type": "int"}]}],
    },
]


def test_mapping_matched_scenario_uses_injected_candidate_ids(monkeypatch, tmp_path):
    """字典命中夹具：真实阅读 catalog_subset，按操作名取注入 ID 产出 mapped 步骤。"""
    monkeypatch.setenv("FAKE_GLM_MAPPING", "matched")
    wd = _mapping_workdir(tmp_path, _DEMO_SUBSET)
    proc = _run(wd, "mapping-skill")
    assert proc.returncode == 0, proc.stderr

    result = json.loads((wd / "result.json").read_text(encoding="utf-8"))
    steps = result["steps"]
    assert [s["mapping_status"] for s in steps] == ["mapped", "mapped"]
    assert steps[0]["aw_operation_id"] == 1
    assert steps[1]["aw_operation_id"] == 2
    assert steps[1]["params"] == {"command": "DSP_CELL", "args": {"cell_id": 1}}


def test_mapping_intercepted_scenario_emits_command_unknown_to_dictionary(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("FAKE_GLM_MAPPING", "intercepted")
    wd = _mapping_workdir(tmp_path, _DEMO_SUBSET)
    proc = _run(wd, "mapping-skill")
    assert proc.returncode == 0, proc.stderr

    generic_step = json.loads((wd / "result.json").read_text(encoding="utf-8"))["steps"][1]
    assert generic_step["mapping_status"] == "mapped"  # LLM 自报合法
    assert generic_step["params"]["command"] == "LST_BOGUS"  # 字典不认，待服务端拦截


def test_mapping_rejects_subset_entry_missing_kind(monkeypatch, tmp_path):
    """注入契约守护：候选条目不带 kind/device_target 时夹具拒绝产出。"""
    monkeypatch.setenv("FAKE_GLM_MAPPING", "matched")
    bad_subset = [{"id": 1, "name": "act_cell", "device_target": "bbu"}]
    wd = _mapping_workdir(tmp_path, bad_subset)
    proc = _run(wd, "mapping-skill")
    assert proc.returncode != 0
    assert "kind" in proc.stderr
    assert not (wd / "result.json").exists()


def test_mapping_rejects_generic_entry_without_commands_fragment(
    monkeypatch, tmp_path
):
    """mml_generic 候选必须挂命令字典片段（即使字典缺失也是空列表，不能缺字段）。"""
    monkeypatch.setenv("FAKE_GLM_MAPPING", "matched")
    bad_subset = [
        {"id": 2, "name": "run_mml", "kind": "mml_generic", "device_target": "bbu"}
    ]
    wd = _mapping_workdir(tmp_path, bad_subset)
    proc = _run(wd, "mapping-skill")
    assert proc.returncode != 0
    assert "commands" in proc.stderr


def test_mapping_fault_scenarios(monkeypatch, tmp_path):
    wd = _mapping_workdir(tmp_path, _DEMO_SUBSET)

    monkeypatch.setenv("FAKE_GLM_MAPPING", "fail")
    assert _run(wd, "mapping-skill").returncode != 0
    assert not (wd / "result.json").exists()

    monkeypatch.setenv("FAKE_GLM_MAPPING", "malformed")
    assert _run(wd, "mapping-skill").returncode == 0
    with pytest.raises(json.JSONDecodeError):
        json.loads((wd / "result.json").read_text(encoding="utf-8"))


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


def test_elaboration_sufficient_scenario(monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_GLM_ELABORATION", "sufficient")
    wd = _prepare_workdir(tmp_path, {"properties": {"sufficient": {}}})
    proc = _run(wd, "elaboration-skill")
    assert proc.returncode == 0, proc.stderr
    result = json.loads((wd / "result.json").read_text(encoding="utf-8"))
    assert result["sufficient"] is True
    assert result["missing_points"] == []


def test_elaboration_fail_scenario_exits_nonzero(monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_GLM_ELABORATION", "fail")
    wd = _prepare_workdir(tmp_path, {"properties": {"sufficient": {}}})
    proc = _run(wd, "elaboration-skill")
    assert proc.returncode != 0
    assert not (wd / "result.json").exists()


def test_elaboration_malformed_scenario_writes_bad_json(monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_GLM_ELABORATION", "malformed")
    wd = _prepare_workdir(tmp_path, {"properties": {"sufficient": {}}})
    proc = _run(wd, "elaboration-skill")
    assert proc.returncode == 0
    with pytest.raises(json.JSONDecodeError):
        json.loads((wd / "result.json").read_text(encoding="utf-8"))


def test_elaboration_write_and_hang_scenario_writes_result_before_sleep(monkeypatch, tmp_path):
    """契约守护：CLI 可以写完 result.json 后仍不退出；后端按文件存在性取结果。"""
    import time

    monkeypatch.setenv("FAKE_GLM_ELABORATION", "write_and_hang")
    monkeypatch.setenv("FAKE_GLM_SLEEP_SECONDS", "30")
    wd = _prepare_workdir(tmp_path, {"properties": {"sufficient": {}}})
    proc = subprocess.Popen(
        [sys.executable, str(FIXTURE), "--skill", "elaboration-skill", "--workdir", str(wd)]
    )
    try:
        deadline = time.monotonic() + 5
        while not (wd / "result.json").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        result = json.loads((wd / "result.json").read_text(encoding="utf-8"))
        assert result["sufficient"] is True
        # 结果已就绪时进程仍挂起
        assert proc.poll() is None
    finally:
        proc.terminate()
        proc.wait(timeout=10)
