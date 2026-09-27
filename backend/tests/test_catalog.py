"""系统级测试：操作目录加载、检索与场景库索引（T3）。

守护 API 外部行为（spec 故事 35、36、51）：演示目录随应用启动自动加载，
GET /operations?q= 供确认态下拉与映射候选共用唯一数据源；MBB 无查询 API
时场景索引为空、支持手工录入降级。
"""

ALL_KINDS = {
    "mml_family",
    "mml_generic",
    "long_running",
    "instrument_primitive",
    "composite",
}


def _ue_attach() -> dict:
    """合法的最小 instrument_primitive 条目（导入用例）。"""
    return {
        "name": "ue_attach",
        "description": "UE 开机附着网络（演示导入）",
        "kind": "instrument_primitive",
        "device_target": "ue",
        "params_schema": {"type": "object", "properties": {}},
        "simulatable": "schema_stub",
    }


# ---------------------------------------------------------------------------
# 操作目录：加载与检索
# ---------------------------------------------------------------------------


def test_demo_catalog_auto_loaded_with_all_five_kinds(client):
    resp = client.get("/operations")
    assert resp.status_code == 200

    items = resp.json()
    kinds = {item["kind"] for item in items}
    assert kinds == ALL_KINDS
    for item in items:
        assert item["name"]
        assert item["device_target"] in {"bbu", "ue", "instrument", "mbb"}
        assert item["simulatable"] in {"schema_stub", "declarative", "python", "none"}
        assert isinstance(item["params_schema"], dict)


def test_get_operations_search_by_name(client):
    resp = client.get("/operations", params={"q": "scenario"})
    assert resp.status_code == 200

    names = [item["name"] for item in resp.json()]
    assert names == ["play_scenario"]


def test_get_operations_search_by_description(client):
    resp = client.get("/operations", params={"q": "射频输出功率"})
    assert resp.status_code == 200

    names = [item["name"] for item in resp.json()]
    assert names == ["set_rf_power"]


def test_get_operations_q_no_match_returns_empty(client):
    resp = client.get("/operations", params={"q": "zzz-不存在的操作"})
    assert resp.status_code == 200
    assert resp.json() == []


def test_composite_entry_declares_suboperations(client):
    resp = client.get("/operations", params={"q": "play_scenario"})
    item = resp.json()[0]
    assert item["kind"] == "composite"
    assert item["suboperations"] == [
        "resolve_scenario_file",
        "upload_to_instrument",
        "play_file",
        "wait_ready",
    ]


def test_long_running_entry_declares_stages_and_artifacts(client):
    resp = client.get("/operations", params={"q": "export_logs"})
    item = resp.json()[0]
    assert item["kind"] == "long_running"
    assert item["stages"] == ["start", "collect", "finish"]
    assert item["produces_artifacts"] is True


# ---------------------------------------------------------------------------
# 操作目录：导入与非法条目拒绝
# ---------------------------------------------------------------------------


def test_import_operations_adds_new_entries(client):
    resp = client.post("/operations/import", json=[_ue_attach()])
    assert resp.status_code == 201
    assert resp.json() == {"imported": 1}

    resp = client.get("/operations", params={"q": "ue_attach"})
    assert len(resp.json()) == 1


def test_import_operations_reimport_updates_by_name(client):
    entry = _ue_attach()
    entry["description"] = "更新后的描述"
    resp = client.post("/operations/import", json=[entry])
    assert resp.status_code == 201

    items = client.get("/operations", params={"q": "ue_attach"}).json()
    assert len(items) == 1
    assert items[0]["description"] == "更新后的描述"


def test_import_operations_rejects_invalid_entry_atomically(client):
    bad = dict(_ue_attach(), name="bad_op", kind="mml")
    resp = client.post("/operations/import", json=[_ue_attach(), bad])
    assert resp.status_code == 422

    detail = resp.json()["detail"]
    assert any(err["index"] == 1 for err in detail)
    # 原子性：任一条目非法则整批拒绝，不留下半批数据
    assert client.get("/operations", params={"q": "ue_attach"}).json() == []


def test_import_operations_rejects_composite_without_suboperations(client):
    entry = {
        "name": "bad_composite",
        "description": "缺少子操作序列的组合操作",
        "kind": "composite",
        "device_target": "instrument",
        "params_schema": {},
        "simulatable": "none",
    }
    resp = client.post("/operations/import", json=[entry])
    assert resp.status_code == 422


def test_import_operations_rejects_long_running_without_artifacts_declaration(client):
    entry = {
        "name": "bad_long_running",
        "description": "缺少制品声明的长时操作",
        "kind": "long_running",
        "device_target": "bbu",
        "params_schema": {},
        "simulatable": "none",
        "stages": ["start"],
    }
    resp = client.post("/operations/import", json=[entry])
    assert resp.status_code == 422


def test_import_operations_rejects_python_sim_without_package_version(client):
    entry = dict(_ue_attach(), simulatable="python", sim_ref="sims/ue_attach.py")
    resp = client.post("/operations/import", json=[entry])
    assert resp.status_code == 422


def test_import_operations_rejects_declarative_without_sim_ref(client):
    entry = dict(_ue_attach(), simulatable="declarative")
    resp = client.post("/operations/import", json=[entry])
    assert resp.status_code == 422


def test_import_operations_rejects_unknown_fields(client):
    entry = dict(_ue_attach(), typo_field="oops")
    resp = client.post("/operations/import", json=[entry])
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 场景库索引：空索引降级、手工录入、检索与同步
# ---------------------------------------------------------------------------


def test_scenarios_index_empty_by_default(client):
    """MBB 无查询 API 时索引为空，确认态降级为手工录入。"""
    resp = client.get("/scenarios")
    assert resp.status_code == 200
    assert resp.json() == []


def test_manual_scenario_entry_visible_in_index(client):
    payload = {
        "scenario_id": "SCN-001",
        "version": "v2.1",
        "name": "城区蜂窝容量场景",
        "template_type": "cellular_capacity",
        "has_meta": True,
    }
    resp = client.post("/scenarios", json=payload)
    assert resp.status_code == 201
    assert resp.json()["scenario_id"] == "SCN-001"

    items = client.get("/scenarios").json()
    assert len(items) == 1
    assert items[0]["template_type"] == "cellular_capacity"
    assert items[0]["has_meta"] is True


def test_manual_scenario_duplicate_id_version_rejected(client):
    payload = {
        "scenario_id": "SCN-001",
        "version": "v2.1",
        "name": "城区蜂窝容量场景",
    }
    assert client.post("/scenarios", json=payload).status_code == 201

    resp = client.post("/scenarios", json=payload)
    assert resp.status_code == 409


def test_scenarios_search_by_name_and_template_type(client):
    client.post(
        "/scenarios",
        json={
            "scenario_id": "SCN-001",
            "version": "v2.1",
            "name": "城区蜂窝容量场景",
            "template_type": "cellular_capacity",
        },
    )
    client.post(
        "/scenarios",
        json={
            "scenario_id": "SCN-002",
            "version": "v1.0",
            "name": "高速铁路穿透场景",
            "template_type": "hst_penetration",
        },
    )

    by_name = client.get("/scenarios", params={"q": "铁路"}).json()
    assert [i["scenario_id"] for i in by_name] == ["SCN-002"]

    by_template = client.get("/scenarios", params={"q": "cellular"}).json()
    assert [i["scenario_id"] for i in by_template] == ["SCN-001"]


def test_scenario_import_syncs_full_index(client):
    """MBB 有查询 API 时整库同步：每次导入以最新快照替换全量索引。"""
    first = [
        {"scenario_id": "SCN-001", "version": "v1", "name": "场景一"},
        {"scenario_id": "SCN-002", "version": "v1", "name": "场景二"},
    ]
    assert client.post("/scenarios/import", json=first).status_code == 201
    assert len(client.get("/scenarios").json()) == 2

    second = [{"scenario_id": "SCN-003", "version": "v1", "name": "场景三"}]
    assert client.post("/scenarios/import", json=second).status_code == 201

    ids = [i["scenario_id"] for i in client.get("/scenarios").json()]
    assert ids == ["SCN-003"]


def test_scenario_import_rejects_missing_version(client):
    resp = client.post(
        "/scenarios/import", json=[{"scenario_id": "SCN-001", "name": "无版本号"}]
    )
    assert resp.status_code == 422
    assert client.get("/scenarios").json() == []
