"""系统级测试：参数化用例进化（T14，Issue #15，故事 23–27；ADR-0008）。

守护的验收标准：
- 可将用例标记为种子（MVP 手工认证 origin=seed）并维护可变参数槽位定义
  （频段/功率/UE 数等）
- POST evolve 接收槽位值，复制种子结构化步骤并替换参数，生成 origin=evolved、
  parent_case_id 指向种子的新用例
- 进化过程不调用 GLM、不产生新的映射不确定性（绕过扩写+映射）
- 可查看种子→进化血缘关系，五环追溯自动串联
- 进化行为（含槽位替换、血缘字段）经端到端测试守护
"""
from app.db import SessionLocal
from app.evolution import substitute
from app.models import TextCase
from app.models.mapping import StructuredStep

from tests.test_execution import _make_executable_case


# ---------------------------------------------------------------------------
# 纯函数单测：占位符替换
# ---------------------------------------------------------------------------


def test_substitute_replaces_full_match_preserving_type():
    """整段等于 {{slot}} → 替换为原值（保留 int/float/bool 类型流入 params）。"""
    assert substitute("{{freq_band}}", {"freq_band": "n78"}) == "n78"
    # int 参数流入 params.cell_id 时保留类型，便于后续 JSON Schema 校验
    assert substitute("{{cell_id}}", {"cell_id": 5}) == 5
    assert substitute("{{power}}", {"power": 40.5}) == 40.5
    assert substitute("{{flag}}", {"flag": True}) is True


def test_substitute_replaces_substring_within_strings():
    """字符串内嵌占位符 → 子串替换，结果恒为字符串。"""
    out = substitute("频段 {{freq_band}} 小区 {{cell_id}}", {"freq_band": "n78", "cell_id": 5})
    assert out == "频段 n78 小区 5"


def test_substitute_recurses_into_dicts_and_lists():
    """params 是嵌套 JSON：dict/list 递归替换；未声明的占位符原样保留。"""
    params = {
        "command": "DSP_CELL",
        "args": {"cell_id": "{{cell_id}}", "label": "cell-{{cell_id}}"},
        "extras": ["{{cell_id}}", {"nested": "cell-{{cell_id}}"}],
    }
    out = substitute(params, {"cell_id": 7})
    assert out["args"]["cell_id"] == 7  # 整段 → 原值
    assert out["args"]["label"] == "cell-7"  # 子串 → 字符串
    assert out["extras"][0] == 7
    assert out["extras"][1]["nested"] == "cell-7"


def test_substitute_leaves_unknown_placeholders_untouched():
    """未在 slot_values 中提供的 {{unknown}} 原样保留——校验在 evolve 入口完成。"""
    assert substitute("{{unknown}}", {"freq_band": "n78"}) == "{{unknown}}"


def test_substitute_passes_through_non_strings_unchanged():
    """非 str/dict/list 原样返回。"""
    assert substitute(42, {"x": 1}) == 42
    assert substitute(None, {"x": 1}) is None
    assert substitute(True, {"x": 1}) is True


# ---------------------------------------------------------------------------
# 种子认证（POST /text-cases/{id}/mark-seed）
# ---------------------------------------------------------------------------


def test_mark_seed_sets_origin_and_writes_slots(client, fake_cli, fake_lass, monkeypatch):
    """标记种子：origin=seed、variable_slots 落库；用例状态不变（仍可走 generate/execute）。"""
    made = _make_executable_case(client, fake_cli, monkeypatch)
    case_id = made["case"]["id"]

    resp = client.post(
        f"/text-cases/{case_id}/mark-seed",
        json={"variable_slots": {"freq_band": "频段", "power": "功率dBm"}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["origin"] == "seed"
    assert body["variable_slots"] == {"freq_band": "频段", "power": "功率dBm"}
    # 状态不回退（generate 已置 generated；mark-seed 不应推动状态机）
    assert body["status"] == "generated"

    # 落库持久化
    persisted = client.get(f"/text-cases/{case_id}").json()
    assert persisted["origin"] == "seed"
    assert persisted["variable_slots"]["freq_band"] == "频段"


def test_mark_seed_resubmit_replaces_slots_wholesale(client, fake_cli, fake_lass, monkeypatch):
    """重提交整体替换槽位定义（不增量合并）——允许调整槽位集。"""
    made = _make_executable_case(client, fake_cli, monkeypatch)
    case_id = made["case"]["id"]

    client.post(
        f"/text-cases/{case_id}/mark-seed",
        json={"variable_slots": {"freq_band": "频段", "power": "功率dBm"}},
    )
    # 调整槽位集：去掉 power、新增 ue_count
    resp = client.post(
        f"/text-cases/{case_id}/mark-seed",
        json={"variable_slots": {"freq_band": "频段", "ue_count": "UE 数量"}},
    )
    assert resp.status_code == 200
    assert resp.json()["variable_slots"] == {"freq_band": "频段", "ue_count": "UE 数量"}


def test_mark_seed_allows_empty_slots_for_static_seed(client, fake_cli, fake_lass, monkeypatch):
    """全静态种子（无参数化分量）也可作进化基准：variable_slots 为空字典。"""
    made = _make_executable_case(client, fake_cli, monkeypatch)
    case_id = made["case"]["id"]

    resp = client.post(
        f"/text-cases/{case_id}/mark-seed", json={"variable_slots": {}}
    )
    assert resp.status_code == 200
    assert resp.json()["variable_slots"] == {}
    assert resp.json()["origin"] == "seed"


def test_mark_seed_unknown_case_404(client):
    assert client.post("/text-cases/9999/mark-seed", json={"variable_slots": {}}).status_code == 404


def test_mark_seed_rejects_unknown_field(client, fake_cli, fake_lass, monkeypatch):
    """schema extra=forbid：拼写错误的字段一律 422（与全仓 schema 一致）。"""
    made = _make_executable_case(client, fake_cli, monkeypatch)
    case_id = made["case"]["id"]
    resp = client.post(
        f"/text-cases/{case_id}/mark-seed",
        json={"variable_slots": {}, "extra_field": "x"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 用例进化（POST /text-cases/{id}/evolve）：happy path + 血缘
# ---------------------------------------------------------------------------


def _inject_placeholders_into_seed(case_id: int, slot_names: list[str]) -> None:
    """直接落库给种子的文本/步骤注入 {{slot}} 占位符（绕开 GLM 映射夹具的固定输出）。

    _make_executable_case 经 fake CLI 映射产出固定的 step params（act_cell 的
    {cell_id: 1} 等），与参数化进化场景无关；本助手把种子文本与结构化步骤
    params/action_text/assertion_text 改写为带占位符的形状，让 evolve 的替换
    逻辑有真实信号可断言。

    设计约束（避免破坏后续 generate 渲染闸门）：
    - 文本字段（title/precondition/steps_text/expected_text）一律注入第一个
      槽位的占位符——文本字段无形状约束。
    - 结构化步骤 params 只在槽位名命中现有 param key 时替换该 key 的值为
      ``{{slot_name}}``；否则保留原值。这样 act_cell 的 required ``cell_id``
      仍存在，render 闸门不会因缺字段 422。
    - action_text/assertion_text 追加 ``（{{slot}}）`` 后缀（不影响渲染，纯展示）。

    slot_names 必须与 mark-seed 声明的槽位名一致，否则 evolve 的
    undeclared_placeholders 守卫会 422 拒绝（守护测试自身的正确性）。
    """
    if not slot_names:
        return
    fb = slot_names[0]
    second = slot_names[1] if len(slot_names) > 1 else fb
    slot_set = set(slot_names)

    def _subtree(value):
        """递归把 dict/list 中命中的 slot_name 键值替换为 {{slot_name}} 占位符。"""
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                if k in slot_set and not isinstance(v, (dict, list)):
                    out[k] = f"{{{{{k}}}}}"
                else:
                    out[k] = _subtree(v)
            return out
        if isinstance(value, list):
            return [_subtree(v) for v in value]
        return value

    db = SessionLocal()
    try:
        case = db.get(TextCase, case_id)
        assert case is not None
        case.title = f"切换 {{{{{fb}}}}} 验证"
        case.precondition = f"基站已加电，目标频段 {{{{{fb}}}}}"
        case.steps_text = f"1. 激活 {{{{{fb}}}}} 小区\n2. 设置参数 {{{{{second}}}}}"
        case.expected_text = f"1. 小区状态为激活\n2. 参数读数为 {{{{{second}}}}}"

        steps = (
            db.query(StructuredStep)
            .filter(StructuredStep.text_case_id == case_id)
            .order_by(StructuredStep.seq)
            .all()
        )
        for step in steps:
            step.action_text = step.action_text + f"（{{{{{fb}}}}}）"
            step.assertion_text = step.assertion_text + f"（{{{{{second}}}}}）"
            step.params = _subtree(step.params)
        db.commit()
    finally:
        db.close()


def _make_seed_with_slots(
    client, fake_cli, monkeypatch, slots: dict[str, str]
) -> dict:
    """全链路造 confirmed/generated 态用例 → mark-seed → 注入占位符 → 返回 case_id。

    slots 是 mark-seed 的槽位定义（name → description）；本助手把 keys 当作
    占位符名注入种子，保证 evolve 时 slot_values 与声明的槽位一一对应。

    返回 {"case_id": int, "exec_id": int}：exec_id 仅供前置测试复用。
    """
    made = _make_executable_case(client, fake_cli, monkeypatch)
    case_id = made["case"]["id"]
    assert client.post(
        f"/text-cases/{case_id}/mark-seed", json={"variable_slots": slots}
    ).status_code == 200
    _inject_placeholders_into_seed(case_id, list(slots.keys()))
    return {"case_id": case_id, "exec_id": made["executable"]["id"]}


def test_evolve_copies_structured_steps_and_substitutes_placeholders(
    client, fake_cli, fake_lass, monkeypatch
):
    """happy path：复制种子的结构化步骤与文本，替换 {{slot}} 占位符为值。"""
    seed = _make_seed_with_slots(
        client, fake_cli, monkeypatch,
        slots={"freq_band": "频段", "power": "功率dBm", "cell_id": "小区号"},
    )

    resp = client.post(
        f"/text-cases/{seed['case_id']}/evolve",
        json={"slot_values": {"freq_band": "n78", "power": 40, "cell_id": 5}},
    )
    assert resp.status_code == 201, resp.text
    evolved = resp.json()
    assert evolved["origin"] == "evolved"
    assert evolved["parent_case_id"] == seed["case_id"]
    # 文本占位符替换
    assert evolved["title"] == "切换 n78 验证"
    assert evolved["precondition"] == "基站已加电，目标频段 n78"
    assert "激活 n78 小区" in evolved["steps_text"]
    assert "参数读数为 40" in evolved["expected_text"]
    # 状态停在 mapped（继承种子的映射结论，绕过扩写+映射）
    assert evolved["status"] == "mapped"
    # 零 LLM 成本：elaboration_qa/mapping_job 留空
    assert evolved["elaboration"] is None
    assert evolved["mapping"] is None

    # 结构化步骤逐条复制并替换占位符
    steps = client.get(f"/text-cases/{evolved['id']}/steps").json()
    assert len(steps) == 2
    assert steps[0]["action_text"].endswith("（n78）")
    assert steps[0]["assertion_text"].endswith("（40）")
    # 整段占位符 → 保留类型（int 流入 params.cell_id）
    assert steps[0]["params"] == {"cell_id": 5}
    # 嵌套 dict 的占位符替换（args.cell_id 整段占位符 → int 原值）
    assert steps[1]["params"]["args"]["cell_id"] == 5
    # 映射结论继承（mapped/manual 不变，无 unmapped 传染）
    assert all(s["mapping_status"] in ("mapped", "manual") for s in steps)
    # aw_operation_id 与种子一致（操作引用原样继承）
    seed_steps = client.get(f"/text-cases/{seed['case_id']}/steps").json()
    assert [s["aw_operation_id"] for s in steps] == [
        s["aw_operation_id"] for s in seed_steps
    ]


def test_evolve_zero_llm_cost_can_directly_confirm_and_generate(
    client, fake_cli, fake_lass, monkeypatch
):
    """进化用例绕过扩写+映射，可直接 confirm 推进至 generated（不调 GLM）。

    AC：进化过程不调用 GLM，不产生新的映射不确定性。
    """
    seed = _make_seed_with_slots(
        client, fake_cli, monkeypatch, slots={"freq_band": "频段"}
    )

    evolved = client.post(
        f"/text-cases/{seed['case_id']}/evolve",
        json={"slot_values": {"freq_band": "n78"}},
    ).json()

    # 不需要再 /elaboration/skip、/map：直接 confirm 闸门通过
    confirm = client.post(f"/text-cases/{evolved['id']}/confirm")
    assert confirm.status_code == 200
    assert confirm.json()["status"] == "confirmed"

    gen = client.post(f"/text-cases/{evolved['id']}/generate")
    assert gen.status_code == 201
    assert gen.json()["version"] == 1  # 进化用例首次生成，version 从 1 起


def test_evolve_trace_chain_auto_chains_seed_evolution_rings(
    client, fake_cli, fake_lass, monkeypatch
):
    """AC：可查看种子→进化血缘关系，五环追溯自动串联。

    进化用例生成可执行用例后，GET /executable-cases/{id}/trace 的
    seed_case=种子、evolution_case=当前进化用例——无需额外接线（T13 trace
    端点已按 parent_case_id 取两环，T14 落地即串联）。
    """
    seed = _make_seed_with_slots(
        client, fake_cli, monkeypatch, slots={"freq_band": "频段"}
    )
    evolved = client.post(
        f"/text-cases/{seed['case_id']}/evolve",
        json={"slot_values": {"freq_band": "n78"}},
    ).json()
    client.post(f"/text-cases/{evolved['id']}/confirm")
    exec_id = client.post(f"/text-cases/{evolved['id']}/generate").json()["id"]

    trace = client.get(f"/executable-cases/{exec_id}/trace").json()
    assert trace["evolution_case"]["id"] == evolved["id"]
    assert trace["seed_case"]["id"] == seed["case_id"]
    # 进化用例的文本已被占位符替换（标题可见）
    evolved_case = client.get(f"/text-cases/{evolved['id']}").json()
    assert evolved_case["title"] == "切换 n78 验证"
    # 结构化环：步骤原样继承（freq_band 不命中 act_cell 的 cell_id param key）
    assert trace["structured_steps"][0]["params"] == {"cell_id": 1}


def test_evolve_inherits_required_topology(client, fake_cli, fake_lass, monkeypatch):
    """种子声明的所需拓扑原样继承（拓扑是环境元数据，与槽位无关）。"""
    made = _make_executable_case(client, fake_cli, monkeypatch)
    case_id = made["case"]["id"]
    # 给种子注入拓扑声明（执行前置条件，与槽位并行）
    db = SessionLocal()
    try:
        case = db.get(TextCase, case_id)
        assert case is not None
        case.required_topology = {"bbu": 1, "ue": 2}
        db.commit()
    finally:
        db.close()

    client.post(
        f"/text-cases/{case_id}/mark-seed",
        json={"variable_slots": {"freq_band": "频段"}},
    )
    evolved = client.post(
        f"/text-cases/{case_id}/evolve",
        json={"slot_values": {"freq_band": "n78"}},
    ).json()
    assert evolved["required_topology"] == {"bbu": 1, "ue": 2}


def test_list_evolutions_returns_children_newest_first(
    client, fake_cli, fake_lass, monkeypatch
):
    """GET /text-cases/{id}/evolutions：按创建时间倒序列出种子进化产生的全部用例。"""
    seed = _make_seed_with_slots(
        client, fake_cli, monkeypatch, slots={"freq_band": "频段"}
    )

    e1 = client.post(
        f"/text-cases/{seed['case_id']}/evolve",
        json={"slot_values": {"freq_band": "n78"}},
    ).json()
    e2 = client.post(
        f"/text-cases/{seed['case_id']}/evolve",
        json={"slot_values": {"freq_band": "n41"}},
    ).json()

    listing = client.get(f"/text-cases/{seed['case_id']}/evolutions").json()
    assert [c["id"] for c in listing] == [e2["id"], e1["id"]]
    assert all(c["parent_case_id"] == seed["case_id"] for c in listing)
    assert all(c["status"] == "mapped" for c in listing)


# ---------------------------------------------------------------------------
# 准入闸门（被测试守护）
# ---------------------------------------------------------------------------


def test_evolve_rejects_unmarked_case(client, fake_cli, fake_lass, monkeypatch):
    """未标记为种子的用例不可进化（须先 mark-seed）→ 409。"""
    made = _make_executable_case(client, fake_cli, monkeypatch)
    resp = client.post(
        f"/text-cases/{made['case']['id']}/evolve",
        json={"slot_values": {}},
    )
    assert resp.status_code == 409
    assert "mark-seed" in resp.json()["detail"]


def test_evolve_rejects_non_confirmed_or_generated_state(
    client, fake_cli, fake_lass, monkeypatch
):
    """种子状态不在 confirmed/generated → 409（结构化步骤未确认，传染 unmapped）。"""
    # 构造一个停留在 draft 态的用例并强行 mark-seed（mark-seed 不看状态）
    case = client.post(
        "/text-cases",
        json={
            "title": "草稿种子",
            "precondition": "x",
            "steps_text": "y",
            "expected_text": "z",
        },
    ).json()
    assert client.post(
        f"/text-cases/{case['id']}/mark-seed",
        json={"variable_slots": {"freq_band": "频段"}},
    ).status_code == 200

    resp = client.post(
        f"/text-cases/{case['id']}/evolve",
        json={"slot_values": {"freq_band": "n78"}},
    )
    assert resp.status_code == 409
    assert "confirmed/generated" in resp.json()["detail"]


def test_evolve_rejects_seed_with_unmapped_steps(client, fake_cli, fake_lass, monkeypatch):
    """种子停留在 mapped 态（含未映射步骤无法 confirm）→ 409。

    验证状态闸门先于步骤检查：含 unmapped 步骤的种子永远到不了
    confirmed/generated 态（confirm 闸门会先拒绝），故 evolve 的状态闸门
    即可挡住"种子含未映射步骤"这一情形；evolve_case 内的 unmapped 步骤
    显式守卫是防御性兜底（防止有人绕过 confirm 直接落库 confirmed 态）。
    """
    # 走到 mapped 态但全 unmapped（fake_cli 默认 unmapped 场景）
    from tests.test_execution import _import_demo_dictionary, _poll_mapping

    _import_demo_dictionary(client)
    # 不设 FAKE_GLM_MAPPING → 默认 unmapped
    case = client.post(
        "/text-cases",
        json={
            "title": "未映射种子",
            "precondition": "基站已加电",
            "steps_text": "1. 激活目标小区",
            "expected_text": "1. 小区状态为激活",
        },
    ).json()
    client.post(f"/text-cases/{case['id']}/elaboration/skip")
    client.post(f"/text-cases/{case['id']}/map")
    job = _poll_mapping(client, case["id"])
    assert job["unmapped_count"] == 1
    # 用例停在 mapped 态（unmapped 步骤阻 confirm）
    assert client.get(f"/text-cases/{case['id']}").json()["status"] == "mapped"

    # 强行 mark-seed（mark-seed 不看步骤是否全 mapped）
    client.post(
        f"/text-cases/{case['id']}/mark-seed",
        json={"variable_slots": {"freq_band": "频段"}},
    )
    # 但用例状态不在 confirmed/generated → 409
    resp = client.post(
        f"/text-cases/{case['id']}/evolve",
        json={"slot_values": {"freq_band": "n78"}},
    )
    assert resp.status_code == 409


def test_evolve_rejects_missing_slot_values(client, fake_cli, fake_lass, monkeypatch):
    """slot_values 缺失种子声明的槽位 → 422。"""
    seed = _make_seed_with_slots(
        client, fake_cli, monkeypatch,
        slots={"freq_band": "频段", "power": "功率dBm"},
    )
    resp = client.post(
        f"/text-cases/{seed['case_id']}/evolve",
        json={"slot_values": {"freq_band": "n78"}},  # 缺 power
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "missing_slot_values"
    assert "power" in resp.json()["detail"]["detail"]


def test_evolve_rejects_extra_slot_values(client, fake_cli, fake_lass, monkeypatch):
    """slot_values 含种子未声明的槽位 → 422（不允许部分进化）。"""
    seed = _make_seed_with_slots(
        client, fake_cli, monkeypatch, slots={"freq_band": "频段"}
    )
    resp = client.post(
        f"/text-cases/{seed['case_id']}/evolve",
        json={"slot_values": {"freq_band": "n78", "extra": "x"}},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "unknown_slot_values"
    assert "extra" in resp.json()["detail"]["detail"]


def test_evolve_rejects_seed_with_undeclared_placeholders(
    client, fake_cli, fake_lass, monkeypatch
):
    """种子含未声明的 {{unknown}} 占位符 → 422（种子标注错误，质量杠杆失效）。

    故事 26：种子质量是全系统质量杠杆——槽位标注错误的种子会批量传染，
    此处显式拒绝避免脏数据流入进化用例。
    """
    seed = _make_seed_with_slots(
        client, fake_cli, monkeypatch, slots={"freq_band": "频段"}
    )
    # 在种子里偷偷加一个未声明的 {{extra_slot}} 占位符
    db = SessionLocal()
    try:
        case = db.get(TextCase, seed["case_id"])
        assert case is not None
        case.precondition = case.precondition + " {{extra_slot}}"
        db.commit()
    finally:
        db.close()

    resp = client.post(
        f"/text-cases/{seed['case_id']}/evolve",
        json={"slot_values": {"freq_band": "n78"}},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "undeclared_placeholders"
    assert "extra_slot" in resp.json()["detail"]["detail"]


def test_evolve_unknown_case_404(client):
    resp = client.post(
        "/text-cases/9999/evolve", json={"slot_values": {}}
    )
    assert resp.status_code == 404


def test_evolve_rejects_unknown_field(client, fake_cli, fake_lass, monkeypatch):
    """schema extra=forbid：拼写错误的字段一律 422。"""
    seed = _make_seed_with_slots(
        client, fake_cli, monkeypatch, slots={"freq_band": "频段"}
    )
    resp = client.post(
        f"/text-cases/{seed['case_id']}/evolve",
        json={"slot_values": {"freq_band": "n78"}, "extra": "x"},
    )
    assert resp.status_code == 422


def test_list_evolutions_unknown_case_404(client):
    assert client.get("/text-cases/9999/evolutions").status_code == 404
