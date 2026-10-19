"""Слой «где можно сажать» — сводка и картинки по группам (см. zones_service.py)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from backend.app.api.deps import PlanDep
from backend.app.schemas.plan import PlanZonesOut, PlantingType
from backend.app.services import zones_service

router = APIRouter(prefix="/api/projects/{project_id}/plans/{plan_id}", tags=["zones"])

_NOT_FOUND = "Для этого плана слой ограничений построить не из чего: у проекта нет границы участка."


@router.get("/zones", response_model=PlanZonesOut)
async def get_zones(plan: PlanDep, planting_type: PlantingType = "tree") -> PlanZonesOut:
    """Доли допустимой площади и зон по ограничениям, цвета и рамка слоя;
    первый вызов считает маски (секунды на крупной улице), следующие — из кэша."""
    summary = await run_in_threadpool(zones_service.zones_summary, plan, planting_type)
    if summary is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return PlanZonesOut(**summary)


@router.get("/zones/image")
async def get_zone_image(plan: PlanDep, group: str, planting_type: PlantingType = "tree") -> Response:
    png = await run_in_threadpool(zones_service.zone_png, plan, planting_type, group)
    if png is None:
        raise HTTPException(status_code=404, detail=f"Группа «{group}» в слое ограничений не найдена.")
    return Response(content=png, media_type="image/png")
