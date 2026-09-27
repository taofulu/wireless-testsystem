"""目录域路由：操作目录检索/导入、命令字典导入、场景库索引（T3）。

链路：确认态 UI / 映射 skill → /operations、/scenarios → DB 唯一主存。
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.catalog import (
    CatalogError,
    ScenarioConflict,
    add_scenario_manual,
    import_operations,
    operation_to_out,
    replace_dictionary,
    sync_scenarios,
)
from app.db import get_db
from app.models.catalog import Operation, Scenario
from app.schemas import ScenarioOut

router = APIRouter(tags=["catalog"])


@router.post("/operations/import", status_code=201)
def import_operation_catalog(entries: list[dict], db: Session = Depends(get_db)):
    """机器可读目录导入：任一条目非法则整批拒绝（422 附逐条原因）。"""
    try:
        imported = import_operations(db, entries)
    except CatalogError as exc:
        raise HTTPException(status_code=422, detail=exc.args[0]) from exc
    return {"imported": imported}


@router.get("/operations")
def list_operations(q: Optional[str] = None, db: Session = Depends(get_db)):
    """操作目录检索（按名称/描述），映射候选与确认态下拉的唯一数据源。"""
    stmt = select(Operation).order_by(Operation.name)
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(
            or_(Operation.name.ilike(pattern), Operation.description.ilike(pattern))
        )
    return [operation_to_out(op) for op in db.execute(stmt).scalars().all()]


@router.post("/command-dictionaries/import", status_code=201)
def import_command_dictionary(payload: dict, db: Session = Depends(get_db)):
    """命令字典导入（随 BBU 版本供给）；最新导入版本为当前生效版本。"""
    try:
        return replace_dictionary(db, payload)
    except CatalogError as exc:
        raise HTTPException(status_code=422, detail=exc.args[0]) from exc


@router.post("/scenarios/import", status_code=201)
def import_scenario_index(items: list[dict], db: Session = Depends(get_db)):
    """MBB 场景库索引整库同步：以最新快照全量替换。"""
    try:
        imported = sync_scenarios(db, items)
    except CatalogError as exc:
        raise HTTPException(status_code=422, detail=exc.args[0]) from exc
    return {"imported": imported}


@router.post("/scenarios", response_model=ScenarioOut, status_code=201)
def create_scenario_manual(payload: dict, db: Session = Depends(get_db)):
    """手工录入降级：MBB 无查询 API 时按 ID+版本补录场景索引。"""
    try:
        scenario = add_scenario_manual(db, payload)
    except CatalogError as exc:
        raise HTTPException(status_code=422, detail=exc.args[0]) from exc
    except ScenarioConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return scenario


@router.get("/scenarios", response_model=list[ScenarioOut])
def list_scenarios(q: Optional[str] = None, db: Session = Depends(get_db)):
    """场景库索引检索（ID/名称/模板类型）；MBB 无 API 时为空索引。"""
    stmt = select(Scenario).order_by(Scenario.scenario_id, Scenario.version)
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(
            or_(
                Scenario.scenario_id.ilike(pattern),
                Scenario.name.ilike(pattern),
                Scenario.template_type.ilike(pattern),
            )
        )
    return db.execute(stmt).scalars().all()
