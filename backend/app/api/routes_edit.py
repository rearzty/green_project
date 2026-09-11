from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

from backend.app.api.deps import PlanDep, SessionDep, get_item_or_404
from backend.app.schemas.geo import GeoJSONFeature
from backend.app.schemas.plan import (
    ItemIdsRequest,
    ItemPatch,
    ItemsDeleteResult,
    ItemsMoveRequest,
    ItemsMoveResult,
    ItemsRestoreRequest,
    ItemsRestoreResult,
    ItemsRetypeRequest,
    ItemsRetypeResult,
    ValidateItemsRequest,
    ValidateResponse,
    ValidationViolation,
)
from backend.app.services.edit_service import (
    ItemsAlreadyExistError,
    ItemsNotFoundError,
    OutOfTerritoryError,
    PlantingTypeGeometryMismatchError,
    apply_item_patch,
    delete_item,
    delete_items,
    move_items,
    restore_items,
    retype_items,
    validate_items,
    validate_plan,
)
from backend.app.services.geo_io import planting_item_to_geojson_feature

router = APIRouter(prefix="/api/projects/{project_id}/plans/{plan_id}", tags=["edit"])


@contextmanager
def _edit_errors() -> Iterator[None]:
    """Maps edit_service's domain errors to HTTP statuses. Their messages are
    already user-facing Russian text, passed through as `detail`."""
    try:
        yield
    except (PlantingTypeGeometryMismatchError, OutOfTerritoryError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ItemsNotFoundError, ItemsAlreadyExistError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=f"Не хватает параметра запроса: {exc}") from exc


@router.patch("/items/{item_id}", response_model=GeoJSONFeature)
async def patch_item(item_id: str, patch: ItemPatch, plan: PlanDep, session: SessionDep) -> GeoJSONFeature:
    item = get_item_or_404(plan, item_id)
    geometry_dict = patch.geometry.model_dump() if patch.geometry is not None else None
    with _edit_errors():
        updated = await apply_item_patch(session, item, geometry_dict, patch.planting_type, patch.species, plan.project.source_crs)
    return planting_item_to_geojson_feature(updated, plan.project.source_crs)


@router.delete("/items/{item_id}", status_code=204)
async def remove_item(item_id: str, plan: PlanDep, session: SessionDep) -> None:
    item = get_item_or_404(plan, item_id)
    await delete_item(session, item)


@router.post("/items/delete", response_model=ItemsDeleteResult)
async def delete_items_route(request: ItemIdsRequest, plan: PlanDep, session: SessionDep) -> ItemsDeleteResult:
    with _edit_errors():
        snapshots = await delete_items(session, plan, request.ids, plan.project.source_crs)
    return ItemsDeleteResult(deleted_items=snapshots, item_count=plan.item_count)


@router.post("/items/retype", response_model=ItemsRetypeResult)
async def retype_items_route(request: ItemsRetypeRequest, plan: PlanDep, session: SessionDep) -> ItemsRetypeResult:
    with _edit_errors():
        previous, skipped = await retype_items(
            session, plan, [(change.id, change.planting_type) for change in request.changes], plan.project.source_crs
        )
    return ItemsRetypeResult(previous_items=previous, skipped_ids=skipped)


@router.post("/items/move", response_model=ItemsMoveResult)
async def move_items_route(request: ItemsMoveRequest, plan: PlanDep, session: SessionDep) -> ItemsMoveResult:
    with _edit_errors():
        moved = await move_items(
            session,
            plan,
            request.ids,
            (request.from_point.x, request.from_point.y),
            (request.to_point.x, request.to_point.y),
            plan.project.source_crs,
        )
    return ItemsMoveResult(items=moved)


@router.post("/items/restore", response_model=ItemsRestoreResult)
async def restore_items_route(request: ItemsRestoreRequest, plan: PlanDep, session: SessionDep) -> ItemsRestoreResult:
    """Undo's counterpart to item deletion -- recreates the exact items in
    `request.items` (a delete's `deleted_items` sent right back)."""
    with _edit_errors():
        await restore_items(session, plan, [f.model_dump() for f in request.items], plan.project.source_crs)
    return ItemsRestoreResult(item_count=plan.item_count)


@router.post("/validate", response_model=ValidateResponse)
async def validate(plan: PlanDep) -> ValidateResponse:
    # Pure shapely computation, no DB access -- off the event loop the same
    # way generate_plan's geometry work is, so a big plan's worth of checks
    # doesn't stall every other request meanwhile. plan.project already has
    # layers eagerly loaded via PlanDep -- no separate ProjectDep here, that
    # used to load the same project+layers a second time over its own query.
    violations = await run_in_threadpool(validate_plan, plan.project, plan)
    return ValidateResponse(violations=[ValidationViolation(**v) for v in violations])


@router.post("/validate/items", response_model=ValidateResponse)
async def validate_items_route(request: ValidateItemsRequest, plan: PlanDep) -> ValidateResponse:
    """Recheck only specific items -- what an edit actually touched -- rather
    than the whole plan (see edit_service.validate_items). The frontend calls
    this after every move/retype/restore instead of a full /validate, merging
    the result into its own violation set by id."""
    violations = await run_in_threadpool(validate_items, plan.project, plan, request.ids)
    return ValidateResponse(violations=[ValidationViolation(**v) for v in violations])
