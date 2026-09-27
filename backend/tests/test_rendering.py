"""纯函数单测：模板渲染（T7，spec Testing Decisions：快照断言覆盖五种 kind）。

渲染器是纯函数——同一输入永远产出同一代码。快照内联锁定，任何模板改动
都必须显式更新快照并说明理由；同时 compile() 校验生成物是合法 Python。
失败分支守护 AC 5：渲染失败给出明确错误（含步骤序号），绝不产出空版本。
"""
import pytest

from app.rendering import RenderError, RenderStep, render_case

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _mml_family(seq: int = 1, **overrides) -> RenderStep:
    base = dict(
        seq=seq,
        action_text="激活目标小区",
        params={"cell_id": 1},
        assertion_text="小区状态为激活",
        op_name="act_cell",
        op_kind="mml_family",
        op_target="bbu",
        required_params=["cell_id"],
    )
    base.update(overrides)
    return RenderStep(**base)


def _mml_generic(seq: int = 2, **overrides) -> RenderStep:
    base = dict(
        seq=seq,
        action_text="动态查询小区状态（通用 MML）",
        params={"command": "DSP_CELL", "args": {"cell_id": 1}},
        assertion_text="返回小区状态",
        op_name="run_mml",
        op_kind="mml_generic",
        op_target="bbu",
        required_params=["command", "args"],
    )
    base.update(overrides)
    return RenderStep(**base)


# ---------------------------------------------------------------------------
# 五种 kind 的快照分支
# ---------------------------------------------------------------------------


def test_snapshot_full_case_mml_family_and_generic():
    """快照：完整用例（mml_family + mml_generic 两步），锁定文件级形状。"""
    code = render_case(
        title="切换频段验证",
        precondition="基站已加电，BBU 与小区状态正常",
        expected_text="1. 小区状态为激活\n2. 返回小区状态",
        steps=[_mml_family(1), _mml_generic(2)],
    )
    assert code == (
        '"""切换频段验证\n'
        "\n"
        "由 Wireless Test System 模板渲染自动生成，禁止手工编辑（ADR-0001）：\n"
        "代码 100% 由结构化步骤渲染产出，修改请回上游文本/确认态后重新生成。\n"
        "\n"
        "预知条件:\n"
        "    基站已加电，BBU 与小区状态正常\n"
        "\n"
        "预期结果:\n"
        "    1. 小区状态为激活\n"
        "    2. 返回小区状态\n"
        '"""\n'
        "import allure\n"
        "\n"
        "import aw\n"
        "\n"
        "\n"
        "@allure.parent_suite('Wireless Test System')\n"
        "@allure.suite('切换频段验证')\n"
        "def test_case() -> None:\n"
        '    """切换频段验证\n'
        '    """\n'
        "    with allure.step('步骤1: 激活目标小区'):\n"
        "        # AW 操作: act_cell (mml_family/bbu)\n"
        "        result_1 = aw.bbu.act_cell(cell_id=1)\n"
        "        assert result_1.ok, '步骤1 断言: 小区状态为激活'\n"
        "    with allure.step('步骤2: 动态查询小区状态（通用 MML）'):\n"
        "        # AW 操作: run_mml (mml_generic/bbu)\n"
        "        result_2 = aw.bbu.run_mml(command='DSP_CELL', args={'cell_id': 1})\n"
        "        assert result_2.ok, '步骤2 断言: 返回小区状态'\n"
    )
    compile(code, "<generated>", "exec")  # 生成物必须是合法 Python


def test_snapshot_long_running_with_artifacts():
    """快照：long_running 分支——调用 + 等待完成 + 制品回收；无断言文本时裸 assert。"""
    step = RenderStep(
        seq=3,
        action_text="导出告警日志",
        params={"log_type": "alarm"},
        assertion_text="",
        op_name="export_logs",
        op_kind="long_running",
        op_target="bbu",
        required_params=["log_type"],
        produces_artifacts=True,
    )
    code = render_case("五类操作覆盖", "环境就绪", "全部步骤成功", [step])
    assert code == (
        '"""五类操作覆盖\n'
        "\n"
        "由 Wireless Test System 模板渲染自动生成，禁止手工编辑（ADR-0001）：\n"
        "代码 100% 由结构化步骤渲染产出，修改请回上游文本/确认态后重新生成。\n"
        "\n"
        "预知条件:\n"
        "    环境就绪\n"
        "\n"
        "预期结果:\n"
        "    全部步骤成功\n"
        '"""\n'
        "import allure\n"
        "\n"
        "import aw\n"
        "\n"
        "\n"
        "@allure.parent_suite('Wireless Test System')\n"
        "@allure.suite('五类操作覆盖')\n"
        "def test_case() -> None:\n"
        '    """五类操作覆盖\n'
        '    """\n'
        "    with allure.step('步骤3: 导出告警日志'):\n"
        "        # AW 操作: export_logs (long_running/bbu)\n"
        "        result_3 = aw.bbu.export_logs(log_type='alarm')\n"
        "        aw.wait_completion(result_3)\n"
        "        aw.collect_artifacts(result_3)  # 制品：testbed 侧路径与校验和\n"
        "        assert result_3.ok\n"
    )
    compile(code, "<generated>", "exec")


def test_snapshot_instrument_primitive():
    """快照：instrument_primitive 分支（仪表命名空间原语调用）。"""
    step = RenderStep(
        seq=4,
        action_text="设置射频功率",
        params={"power_dbm": -10.0},
        assertion_text="功率设置成功",
        op_name="set_rf_power",
        op_kind="instrument_primitive",
        op_target="instrument",
        required_params=["power_dbm"],
    )
    code = render_case("五类操作覆盖", "环境就绪", "全部步骤成功", [step])
    assert "        result_4 = aw.instrument.set_rf_power(power_dbm=-10.0)\n" in code
    assert "        assert result_4.ok, '步骤4 断言: 功率设置成功'\n" in code
    compile(code, "<generated>", "exec")


def test_snapshot_composite_with_suboperations_comment():
    """快照：composite 分支——单次调用（一步骤↔一报告映射）+ 子操作序列注释。

    ADR-0010：子操作不展开为独立 Allure step，注释仅作可读性留痕。
    """
    step = RenderStep(
        seq=5,
        action_text="播放标准场景",
        params={"scenario_id": "SCN-001", "scenario_version": "v2.1"},
        assertion_text="场景播放完成",
        op_name="play_scenario",
        op_kind="composite",
        op_target="instrument",
        required_params=["scenario_id", "scenario_version"],
        suboperations=["resolve", "upload", "play", "wait_ready"],
    )
    code = render_case("五类操作覆盖", "环境就绪", "全部步骤成功", [step])
    assert (
        "        # 有序子操作（对外一个报告映射）: resolve → upload → play → wait_ready\n"
        in code
    )
    assert (
        "        result_5 = aw.instrument.play_scenario("
        "scenario_id='SCN-001', scenario_version='v2.1')\n" in code
    )
    compile(code, "<generated>", "exec")


def test_snapshot_long_running_without_artifacts_has_no_collect_line():
    """produces_artifacts=False 时不渲染制品回收行（目录声明驱动，非硬编码）。"""
    step = RenderStep(
        seq=1,
        action_text="起信令跟踪",
        params={"trace_type": "signalling"},
        op_name="start_trace",
        op_kind="long_running",
        op_target="bbu",
        required_params=["trace_type"],
        produces_artifacts=False,
    )
    code = render_case("T", "P", "E", [step])
    assert "aw.wait_completion(result_1)" in code
    assert "collect_artifacts" not in code


def test_render_determinism_and_param_ordering():
    """params 键序不影响输出（kwargs 按参数名排序，渲染确定性）。"""
    a = _mml_family(params={"cell_id": 1, "band": "n78"})
    b = _mml_family(params={"band": "n78", "cell_id": 1})
    assert render_case("T", "P", "E", [a]) == render_case("T", "P", "E", [b])


def test_render_escapes_docstring_breakers():
    """标题/栏目文本中的三引号与反斜杠不得破坏生成文件的字符串边界。"""
    code = render_case('标题"""注入', "条件\\内容", '预期"""文本', [_mml_family()])
    compile(code, "<generated>", "exec")


def test_render_title_with_trailing_quotes_compiles():
    """标题以引号结尾不破坏函数 docstring 边界（闭合三引号独立成行）。

    单行格式下标题尾部单个 `"` 会与闭合三引号拼出四连引号 → SyntaxError；
    多行格式消除相邻性，单个/双个/三连尾引号都必须可编译。
    """
    for title in ('标题"', '标题""', '标题"""'):
        code = render_case(title, "P", "E", [_mml_family()])
        compile(code, "<generated>", "exec")


# ---------------------------------------------------------------------------
# 失败分支（AC 5：明确错误，不产出空版本）
# ---------------------------------------------------------------------------


def test_missing_required_param_raises_with_step_seq():
    step = _mml_family(params={}, required_params=["cell_id"])
    with pytest.raises(RenderError) as excinfo:
        render_case("T", "P", "E", [step])
    assert excinfo.value.code == "missing_required_params"
    assert excinfo.value.step_seq == 1
    assert "cell_id" in excinfo.value.detail


def test_mml_generic_bad_shape_raises():
    with pytest.raises(RenderError) as empty:
        render_case("T", "P", "E", [_mml_generic(params={"command": "  ", "args": {}})])
    assert empty.value.code == "bad_mml_params"

    with pytest.raises(RenderError) as no_args:
        render_case("T", "P", "E", [_mml_generic(params={"command": "DSP_CELL"})])
    assert no_args.value.code == "missing_required_params"


def test_composite_missing_scenario_freeze_raises():
    """composite 参数未冻结 scenario_id/scenario_version 时拒绝渲染。"""
    step = RenderStep(
        seq=1,
        action_text="播放标准场景",
        params={"scenario_id": "SCN-001"},
        op_name="play_scenario",
        op_kind="composite",
        op_target="instrument",
        required_params=["scenario_id", "scenario_version"],
    )
    with pytest.raises(RenderError) as excinfo:
        render_case("T", "P", "E", [step])
    assert excinfo.value.code == "missing_required_params"
    assert "scenario_version" in excinfo.value.detail


def test_unknown_kind_raises():
    with pytest.raises(RenderError) as excinfo:
        render_case("T", "P", "E", [_mml_family(op_kind="mystery")])
    assert excinfo.value.code == "unknown_kind"


def test_unknown_device_target_raises():
    with pytest.raises(RenderError) as excinfo:
        render_case("T", "P", "E", [_mml_family(op_target="core")])
    assert excinfo.value.code == "unknown_device_target"


def test_empty_steps_raises():
    with pytest.raises(RenderError) as excinfo:
        render_case("T", "P", "E", [])
    assert excinfo.value.code == "empty_steps"


def test_bad_param_name_raises():
    with pytest.raises(RenderError) as excinfo:
        render_case("T", "P", "E", [_mml_family(params={"cell_id": 1, "not-a-name": 1})])
    assert excinfo.value.code == "bad_param_name"
