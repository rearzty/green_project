"""Слой «где можно сажать» — сводка и картинки по группам (см. zones_service.py)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from backend.app.api.deps import PlanDep
from backend.app.schemas.plan import NearbyConstraintsOut, PlanZonesOut, PlantingType
from backend.app.services import constraints_service, zones_service
from backend.app.services.geo_io import display_crs

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


@router.get("/constraints/near", response_model=NearbyConstraintsOut)
async def get_nearby_constraints(
    plan: PlanDep,
    x: float,
    y: float,
    radius: float = constraints_service.DEFAULT_RADIUS_M,
    planting_type: PlantingType = "tree",
    item_id: str | None = None,
) -> NearbyConstraintsOut:
    """Охранные зоны вокруг точки (x, y) — для перетаскивания посадки: сами
    ограничения, их зоны и нормы. Расстояния считает клиент. `item_id` — какую
    посадку тянут (от породы зависят и отступы, и интервал до соседей)."""
    result = await run_in_threadpool(
        constraints_service.nearby_constraints, plan, x, y, radius, planting_type, item_id, display_crs(plan.project)
    )
    return NearbyConstraintsOut(**result)
