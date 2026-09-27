"""命令字典导入与服务端 MML 校验（T3，spec 故事 48-50、ADR-0010）。

导入路径走系统接缝（API）；字典校验器按 spec Testing Decisions 以纯函数
单测守护。字典缺失期间通用 MML 一律不放行——不凭空编造字典。
"""

from app.catalog import check_generic_mml
from app.db import SessionLocal


def _demo_dictionary() -> dict:
    return {
        "version": "bbu-v1.2",
        "commands": [
            {
                "command": "ACT_CELL",
                "description": "激活小区",
                "params": [{"name": "cell_id", "type": "int", "min": 0, "max": 65535}],
            },
            {
                "command": "MOD_CELL",
                "params": [
                    {"name": "cell_id", "type": "int", "min": 0, "max": 65535},
                    {"name": "power_dbm", "type": "float", "min": -60.0, "max": 30.0},
                ],
            },
            {
                "command": "BLK_CELL",
                "params": [
                    {"name": "cell_id", "type": "int", "min": 0, "max": 65535},
                    {
                        "name": "reason",
                        "type": "string",
                        "allowed_values": ["maintenance", "fault"],
                    },
                ],
            },
        ],
    }


def _import_dictionary(client, payload: dict):
    return client.post("/command-dictionaries/import", json=payload)


def _check(command: str, args: dict):
    db = SessionLocal()
    try:
        return check_generic_mml(db, command, args)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------


def test_import_command_dictionary(client):
    resp = _import_dictionary(client, _demo_dictionary())
    assert resp.status_code == 201
    assert resp.json() == {"version": "bbu-v1.2", "imported": 3}


def test_import_command_dictionary_rejects_unknown_param_type(client):
    payload = _demo_dictionary()
    payload["commands"][0]["params"][0]["type"] = "numeric"
    resp = _import_dictionary(client, payload)
    assert resp.status_code == 422


def test_import_command_dictionary_rejects_empty_command_list(client):
    resp = _import_dictionary(client, {"version": "bbu-v1.2", "commands": []})
    assert resp.status_code == 422


def test_import_command_dictionary_rejects_duplicate_command_in_payload(client):
    payload = _demo_dictionary()
    payload["commands"].append(payload["commands"][0])
    resp = _import_dictionary(client, payload)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 服务端校验器
# ---------------------------------------------------------------------------


def test_generic_mml_rejected_when_dictionary_missing(client):
    """字典缺失期间，通用 MML 对所有命令一律不放行。"""
    verdict = _check("ACT_CELL", {"cell_id": 1})
    assert verdict.ok is False
    assert verdict.code == "dictionary_missing"


def test_generic_mml_rejects_unknown_command(client):
    _import_dictionary(client, _demo_dictionary())

    verdict = _check("RESET_BOARD", {})
    assert verdict.ok is False
    assert verdict.code == "unknown_command"


def test_generic_mml_accepts_valid_call(client):
    _import_dictionary(client, _demo_dictionary())

    verdict = _check("MOD_CELL", {"cell_id": 3, "power_dbm": -12.5})
    assert verdict.ok is True
    assert verdict.code == "ok"


def test_generic_mml_rejects_bad_type(client):
    _import_dictionary(client, _demo_dictionary())

    verdict = _check("ACT_CELL", {"cell_id": "one"})
    assert verdict.ok is False
    assert verdict.code == "bad_type"


def test_generic_mml_rejects_out_of_range(client):
    _import_dictionary(client, _demo_dictionary())

    verdict = _check("MOD_CELL", {"cell_id": 1, "power_dbm": 99.0})
    assert verdict.ok is False
    assert verdict.code == "out_of_range"


def test_generic_mml_rejects_missing_and_extra_params(client):
    _import_dictionary(client, _demo_dictionary())

    missing = _check("ACT_CELL", {})
    assert missing.code == "missing_param"

    extra = _check("ACT_CELL", {"cell_id": 1, "bogus": 2})
    assert extra.code == "unknown_param"


def test_generic_mml_rejects_value_not_in_allowed_values(client):
    _import_dictionary(client, _demo_dictionary())

    verdict = _check("BLK_CELL", {"cell_id": 1, "reason": "holidays"})
    assert verdict.ok is False
    assert verdict.code == "value_not_allowed"


def test_reimport_dictionary_switches_active_version(client):
    """最新导入的字典版本生效（版本随 BBU 供给对齐）。"""
    _import_dictionary(client, _demo_dictionary())

    v2 = {
        "version": "bbu-v1.3",
        "commands": [
            {"command": "DEACT_CELL", "params": [{"name": "cell_id", "type": "int"}]}
        ],
    }
    _import_dictionary(client, v2)

    old = _check("ACT_CELL", {"cell_id": 1})
    assert old.ok is False

    new = _check("DEACT_CELL", {"cell_id": 1})
    assert new.ok is True
