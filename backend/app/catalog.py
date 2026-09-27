"""catalog 模块：操作目录加载/导入、命令字典导入与校验、场景库索引（T3）。

职责（spec 模块划分）：
- 操作目录加载与条目校验，非法条目拒绝加载（ADR-0010 分类法）
- 命令字典的导入、版本管理与服务端校验器（字典缺失一律不放行）
- MBB 场景库索引同步（整库快照替换）与手工录入降级
"""
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.catalog import (
    CommandDictionaryEntry,
    DictionaryState,
    Operation,
    Scenario,
)
from app.schemas import DictionaryImportIn, OperationIn, ScenarioIn

# kind 专属声明字段：写入 Operation.extra，查询时展平
_KIND_EXTRA_FIELDS = {
    "composite": ("suboperations",),
    "long_running": ("stages", "produces_artifacts"),
    "mml_generic": ("dictionary_ref",),
}


class CatalogError(Exception):
    """目录域导入失败（非法条目/重复声明），由路由转为 422。"""


# ---------------------------------------------------------------------------
# 操作目录
# ---------------------------------------------------------------------------


def _parse_entries(entries: list[dict]) -> tuple[list[OperationIn], list[dict]]:
    """逐条校验目录条目；返回（合法条目, 错误列表 [{index, name, message}]）。"""
    parsed: list[OperationIn] = []
    errors: list[dict] = []
    seen_names: set[str] = set()
    for index, raw in enumerate(entries):
        try:
            entry = OperationIn.model_validate(raw)
        except ValidationError as exc:
            message = "; ".join(
                f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}"
                for err in exc.errors()
            )
            name = raw.get("name") if isinstance(raw, dict) else None
            errors.append({"index": index, "name": name, "message": message})
            continue
        if entry.name in seen_names:
            errors.append(
                {"index": index, "name": entry.name, "message": "duplicate name in payload"}
            )
        seen_names.add(entry.name)
        parsed.append(entry)
    return parsed, errors


def import_operations(db: Session, entries: list[dict]) -> int:
    """按 name 幂等导入操作目录；任一条目非法则整批拒绝（原子）。"""
    parsed, errors = _parse_entries(entries)
    if errors:
        raise CatalogError(errors)

    for entry in parsed:
        extra = {
            field: getattr(entry, field)
            for field in _KIND_EXTRA_FIELDS.get(entry.kind, ())
        }
        existing = db.execute(
            select(Operation).where(Operation.name == entry.name)
        ).scalar_one_or_none()
        if existing is None:
            existing = Operation(name=entry.name)
            db.add(existing)
        existing.description = entry.description
        existing.kind = entry.kind
        existing.device_target = entry.device_target
        existing.params_schema = entry.params_schema
        existing.simulatable = entry.simulatable
        existing.sim_ref = entry.sim_ref
        existing.sim_package_version = entry.sim_package_version
        existing.extra = extra or None
    db.commit()
    return len(entries)


def load_operations_file(path: Path) -> list[dict]:
    """读取机器可读目录文件（JSON 数组）。"""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise CatalogError(f"catalog file {path} must contain a JSON array")
    return data


def import_operations_from_dir(db: Session, catalog_dir: Union[str, Path]) -> int:
    """应用启动时从目录加载 operations.json；非法条目拒绝加载（启动失败）。"""
    operations_path = Path(catalog_dir) / "operations.json"
    if not operations_path.exists():
        raise CatalogError(f"operations.json not found in {catalog_dir}")
    return import_operations(db, load_operations_file(operations_path))


def operation_to_out(op: Operation) -> dict[str, Any]:
    """核心声明列 + kind 专属 extra 展平为对外视图。"""
    out = {
        "id": op.id,
        "name": op.name,
        "description": op.description,
        "kind": op.kind,
        "device_target": op.device_target,
        "params_schema": op.params_schema,
        "simulatable": op.simulatable,
        "sim_ref": op.sim_ref,
        "sim_package_version": op.sim_package_version,
    }
    out.update(op.extra or {})
    return out


# ---------------------------------------------------------------------------
# 命令字典
# ---------------------------------------------------------------------------


def replace_dictionary(db: Session, payload: dict) -> dict:
    """导入字典：同版本整体替换、并设为当前生效版本（最新供给优先）。"""
    try:
        parsed = DictionaryImportIn.model_validate(payload)
    except ValidationError as exc:
        raise CatalogError(
            [{"index": 0, "name": None, "message": str(exc.errors()[0]["msg"])}]
        ) from exc

    names = [cmd.command for cmd in parsed.commands]
    if len(set(names)) != len(names):
        raise CatalogError(
            [{"index": 0, "name": None, "message": "duplicate command in payload"}]
        )

    db.execute(
        delete(CommandDictionaryEntry).where(
            CommandDictionaryEntry.version == parsed.version
        )
    )
    for cmd in parsed.commands:
        db.add(
            CommandDictionaryEntry(
                version=parsed.version,
                command=cmd.command,
                params=[p.model_dump() for p in cmd.params],
                description=cmd.description,
            )
        )
    state = db.get(DictionaryState, 1)
    if state is None:
        state = DictionaryState(id=1, active_version=parsed.version)
        db.add(state)
    else:
        state.active_version = parsed.version
    db.commit()
    return {"version": parsed.version, "imported": len(parsed.commands)}


# ---------------------------------------------------------------------------
# 服务端 MML 校验器（映射落库前二次校验，不信任 LLM 自报合法）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MMLCheck:
    """通用 MML 调用校验结论；code 供确认态提示分类。"""

    ok: bool
    code: str
    detail: Optional[str] = None


def validate_mml_call(params: Optional[list[dict]], command: str, args: dict) -> MMLCheck:
    """纯函数：按字典参数声明校验一次通用 MML 调用。

    params 为 None/空表示字典缺失——所有命令一律不放行。
    """
    if not params:
        return MMLCheck(False, "dictionary_missing", "command dictionary not imported")

    declared = {p["name"]: p for p in params}
    unknown = sorted(set(args) - set(declared))
    if unknown:
        return MMLCheck(False, "unknown_param", f"unknown params: {', '.join(unknown)}")
    missing = sorted(set(declared) - set(args))
    if missing:
        return MMLCheck(False, "missing_param", f"missing params: {', '.join(missing)}")

    for name, value in args.items():
        spec = declared[name]
        ptype = spec["type"]
        if ptype == "int":
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif ptype == "float":
            valid = isinstance(value, (int, float)) and not isinstance(value, bool)
        elif ptype == "string":
            valid = isinstance(value, str)
        else:  # bool
            valid = isinstance(value, bool)
        if not valid:
            return MMLCheck(False, "bad_type", f"param {name} expects {ptype}")

        if ptype in ("int", "float"):
            low, high = spec.get("min"), spec.get("max")
            if (low is not None and value < low) or (high is not None and value > high):
                return MMLCheck(
                    False, "out_of_range", f"param {name} out of [{low}, {high}]"
                )
        allowed = spec.get("allowed_values")
        if allowed is not None and value not in allowed:
            return MMLCheck(False, "value_not_allowed", f"param {name} not in {allowed}")

    return MMLCheck(True, "ok")


def check_generic_mml(db: Session, command: str, args: dict) -> MMLCheck:
    """对当前生效字典版本做服务端校验；无生效字典即拒绝。"""
    state = db.get(DictionaryState, 1)
    if state is None:
        return MMLCheck(False, "dictionary_missing", "command dictionary not imported")
    entry = db.execute(
        select(CommandDictionaryEntry).where(
            CommandDictionaryEntry.version == state.active_version,
            CommandDictionaryEntry.command == command,
        )
    ).scalar_one_or_none()
    if entry is None:
        return MMLCheck(False, "unknown_command", f"command {command} not in dictionary")
    return validate_mml_call(entry.params, command, args)


# ---------------------------------------------------------------------------
# 场景库索引
# ---------------------------------------------------------------------------


def sync_scenarios(db: Session, items: list[dict]) -> int:
    """整库快照同步：每次导入以最新索引全量替换（MBB 有查询 API 时）。"""
    parsed: list[ScenarioIn] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(items):
        try:
            item = ScenarioIn.model_validate(raw)
        except ValidationError as exc:
            raise CatalogError(
                [{"index": index, "name": raw.get("scenario_id"), "message": str(exc.errors()[0]["msg"])}]
            ) from exc
        key = (item.scenario_id, item.version)
        if key in seen:
            raise CatalogError(
                [{"index": index, "name": item.scenario_id, "message": "duplicate scenario_id/version in payload"}]
            )
        seen.add(key)
        parsed.append(item)

    db.execute(delete(Scenario))
    for item in parsed:
        db.add(
            Scenario(
                scenario_id=item.scenario_id,
                version=item.version,
                name=item.name,
                template_type=item.template_type,
                has_meta=item.has_meta,
            )
        )
    db.commit()
    return len(parsed)


def add_scenario_manual(db: Session, payload: dict) -> Scenario:
    """手工录入降级路径：MBB 无查询 API 时按 ID+版本补录索引。"""
    try:
        item = ScenarioIn.model_validate(payload)
    except ValidationError as exc:
        raise CatalogError(
            [{"index": 0, "name": None, "message": str(exc.errors()[0]["msg"])}]
        ) from exc

    exists = db.execute(
        select(Scenario).where(
            Scenario.scenario_id == item.scenario_id, Scenario.version == item.version
        )
    ).scalar_one_or_none()
    if exists is not None:
        raise ScenarioConflict(item.scenario_id, item.version)

    scenario = Scenario(
        scenario_id=item.scenario_id,
        version=item.version,
        name=item.name,
        template_type=item.template_type,
        has_meta=item.has_meta,
    )
    db.add(scenario)
    db.commit()
    return scenario


class ScenarioConflict(Exception):
    """手工录入的 (scenario_id, version) 已存在。"""

    def __init__(self, scenario_id: str, version: str):
        super().__init__(f"scenario {scenario_id}@{version} already exists")
        self.scenario_id = scenario_id
        self.version = version
