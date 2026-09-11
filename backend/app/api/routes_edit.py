from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

from backend.app.api.deps import PlanDep, ProjectDep, SessionDep, get_item_or_404
from backend.app.schemas.geo import GeoJSONFeature
from backend.app.schemas.plan import ItemPatch, StructuredEditRequest, ValidateResponse, ValidationViolation
from backend.app.services.edit_service import (
    PlantingTypeGeometryMismatchError,
    UnknownOperationError,
    apply_item_patch,
    apply_structured_edit,
    delete_item,
    validate_plan,
)
from backend.app.services.geo_io import planting_item_to_geojson_feature

router = APIRouter(prefix="/api/projects/{project_id}/plans/{plan_id}", tags=["edit"])


@router.patch("/items/{item_id}", response_model=GeoJSONFeature)
async def patch_item(item_id: str, patch: ItemPatch, plan: PlanDep, session: SessionDep) -> GeoJSONFeature:
    item = get_item_or_404(plan, item_id)
    geometry_dict = patch.geometry.model_dump() if patch.geometry is not None else None
    try:
        updated = await apply_item_patch(session, item, geometry_dict, patch.planting_type, patch.species, plan.project.source_crs)
    except PlantingTypeGeometryMismatchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return planting_item_to_geojson_feature(updated, plan.project.source_crs)


@router.delete("/items/{item_id}", status_code=204)
async def remove_item(item_id: str, plan: PlanDep, session: SessionDep) -> None:
    item = get_item_or_404(plan, item_id)
    await delete_item(session, item)


@router.post("/edit-structured", status_code=204)
async def edit_structured(request: StructuredEditRequest, plan: PlanDep, session: SessionDep) -> None:
    try:
        await apply_structured_edit(session, plan, request.operation, request.params, plan.project.source_crs)
    except (UnknownOperationError, PlantingTypeGeometryMismatchError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=f"Missing required param: {exc}") from exc


@router.post("/validate", response_model=ValidateResponse)
async def validate(project: ProjectDep, plan: PlanDep) -> ValidateResponse:
    # Pure shapely computation, no DB access -- off the event loop the same
    # way generate_plan's geometry work is, so a big plan's worth of
    # intersects() checks doesn't stall every other request meanwhile.
    violations = await run_in_threadpool(validate_plan, project, plan)
    return ValidateResponse(violations=[ValidationViolation(**v) for v in violations])
