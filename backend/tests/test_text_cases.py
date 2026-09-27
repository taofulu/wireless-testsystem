"""系统级测试：文本用例三栏录入与草稿持久化（T2）。

守护 UI → API → DB 纵向链路；只测外部行为（HTTP 响应、落库数据）。
领域术语遵循 CONTEXT.md：text_case、precondition、steps_text、expected_text、
status 状态机（初始态 draft）。
"""


def _create_payload(title: str = "RRU 功率核查") -> dict:
    return {
        "title": title,
        "precondition": "基站已加电，BBU 与 RRU 光纤链路正常",
        "steps_text": "1. 查询当前 RRU 发射功率\n2. 修改功率为 40dBm\n3. 再次查询确认生效",
        "expected_text": "1. 返回当前功率值\n2. 命令执行成功\n3. 查询结果为 40dBm",
    }


def test_create_text_case_persists_as_draft(client):
    resp = client.post("/text-cases", json=_create_payload())
    assert resp.status_code == 201

    body = resp.json()
    assert body["id"] is not None
    assert body["title"] == "RRU 功率核查"
    assert body["precondition"] == "基站已加电，BBU 与 RRU 光纤链路正常"
    assert body["steps_text"].startswith("1. 查询当前 RRU 发射功率")
    assert body["expected_text"].endswith("40dBm")
    assert body["status"] == "draft"
    assert body["created_at"] is not None


def test_list_text_cases_returns_created_draft(client):
    client.post("/text-cases", json=_create_payload("用例 A"))
    client.post("/text-cases", json=_create_payload("用例 B"))

    resp = client.get("/text-cases", params={"status": "draft"})
    assert resp.status_code == 200

    items = resp.json()
    assert len(items) == 2
    # 列表按创建时间倒序（最新在前），便于草稿列表回来继续编辑
    assert items[0]["title"] == "用例 B"
    assert items[1]["title"] == "用例 A"
    assert all(item["status"] == "draft" for item in items)


def test_list_text_cases_rejects_unknown_status(client):
    resp = client.get("/text-cases", params={"status": "not-a-state"})
    assert resp.status_code == 422


def test_get_text_case_by_id(client):
    created = client.post("/text-cases", json=_create_payload()).json()

    resp = client.get(f"/text-cases/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]
    assert resp.json()["title"] == "RRU 功率核查"


def test_get_missing_text_case_returns_404(client):
    resp = client.get("/text-cases/9999")
    assert resp.status_code == 404


def test_patch_text_case_updates_content_and_persists(client):
    created = client.post("/text-cases", json=_create_payload()).json()

    patch_resp = client.patch(
        f"/text-cases/{created['id']}",
        json={
            "title": "RRU 功率核查（修订）",
            "steps_text": "1. 查询功率\n2. 修改功率为 43dBm\n3. 复查",
        },
    )
    assert patch_resp.status_code == 200
    patched = patch_resp.json()
    assert patched["title"] == "RRU 功率核查（修订）"
    assert "43dBm" in patched["steps_text"]
    # 未提供的字段保持原值
    assert patched["precondition"] == "基站已加电，BBU 与 RRU 光纤链路正常"
    # 编辑不回退状态
    assert patched["status"] == "draft"

    # 草稿从列表回来重新打开时读到的是持久化后的内容
    reopened = client.get(f"/text-cases/{created['id']}").json()
    assert reopened["title"] == "RRU 功率核查（修订）"
    assert "43dBm" in reopened["steps_text"]


def test_patch_missing_text_case_returns_404(client):
    resp = client.patch("/text-cases/9999", json={"title": "x"})
    assert resp.status_code == 404


def test_create_text_case_rejects_blank_title(client):
    resp = client.post(
        "/text-cases",
        json={"title": "", "precondition": "", "steps_text": "", "expected_text": ""},
    )
    assert resp.status_code == 422


def test_create_partial_draft_with_only_title(client):
    """草稿常是半成品：三栏可留空，稍后回来补（用户故事 2）。"""
    resp = client.post("/text-cases", json={"title": "只写了标题的草稿"})
    assert resp.status_code == 201

    body = resp.json()
    assert body["status"] == "draft"
    assert body["precondition"] == ""
    assert body["steps_text"] == ""
    assert body["expected_text"] == ""


def test_draft_round_trip_from_list_to_editor(client):
    """用户故事 2：保存草稿 → 列表找到 → 打开继续编辑 → 再保存 → 内容持久化。"""
    created = client.post("/text-cases", json=_create_payload("跨时段编写的用例")).json()

    # 稍后从列表回来
    listing = client.get("/text-cases", params={"status": "draft"}).json()
    draft = next(item for item in listing if item["id"] == created["id"])
    assert draft["status"] == "draft"

    # 打开继续编辑并保存
    client.patch(
        f"/text-cases/{draft['id']}",
        json={"expected_text": "补充：功率回读误差不超过 ±0.5dB"},
    )

    final = client.get(f"/text-cases/{draft['id']}").json()
    assert "±0.5dB" in final["expected_text"]
    assert final["steps_text"] == created["steps_text"]
