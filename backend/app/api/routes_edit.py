from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

from backend.app.api.deps import PlanDep, get_item_or_404
from backend.app.schemas.geo import GeoJSONFeature
from backend.app.schemas.plan import (
    AllowedAreaOut,
    FixesApplyRequest,
    FixesApplyResult,
    FixOptionOut,
    ItemFixesOut,
    ItemIdsRequest,
    ItemPatch,
    ItemsDeleteResult,
    ItemsMoveRequest,
    ItemsMoveResult,
    ItemsRespaceRequest,
    ItemsRespaceResult,
    ItemsRestoreRequest,
    ItemsRestoreResult,
    ItemsRetypeRequest,
    ItemsRetypeResult,
    NearestAllowedOut,
    ValidateItemsRequest,
    ValidateResponse,
    ValidationViolation,
)
from backend.app.services.edit_service import (
    ItemsAlreadyExistError,
    ItemsNotFoundError,
    OutOfTerritoryError,
    PlantingTypeGeometryMismatchError,
    RespaceError,
    apply_item_patch,
    delete_item,
    delete_items,
    move_items,
    respace_items,
    restore_items,
    retype_items,
    validate_items_scoped,
    validate_plan,
)
from backend.app.services.fix_service import allowed_area, apply_fixes, item_fixes, nearest_allowed
from backend.app.services.geo_io import display_crs, planting_item_to_geojson_feature

router = APIRouter(prefix="/api/projects/{project_id}/plans/{plan_id}", tags=["edit"])


@contextmanager
def _edit_errors() -> Iterator[None]:
    """Maps edit_service's domain errors to HTTP statuses. Their messages are
    already user-facing Russian text, passed through as `detail`."""
    try:
        yield
    except (PlantingTypeGeometryMismatchError, OutOfTerritoryError, RespaceError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ItemsNotFoundError, ItemsAlreadyExistError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=f"Не хватает параметра запроса: {exc}") from exc


@router.patch("/items/{item_id}", response_model=GeoJSONFeature)
async def patch_item(item_id: str, patch: ItemPatch, plan: PlanDep) -> GeoJSONFeature:
    item = get_item_or_404(plan, item_id)
    geometry_dict = patch.geometry.model_dump() if patch.geometry is not None else None
    with _edit_errors():
        updated = apply_item_patch(item, geometry_dict, patch.planting_type, patch.species, display_crs(plan.project))
    return planting_item_to_geojson_feature(updated, display_crs(plan.project))


@router.delete("/items/{item_id}", status_code=204)
async def remove_item(item_id: str, plan: PlanDep) -> None:
    item = get_item_or_404(plan, item_id)
    delete_item(item)


@router.post("/items/delete", response_model=ItemsDeleteResult)
async def delete_items_route(request: ItemIdsRequest, plan: PlanDep) -> ItemsDeleteResult:
    with _edit_errors():
        snapshots = delete_items(plan, request.ids, display_crs(plan.project))
    return ItemsDeleteResult(deleted_items=snapshots, item_count=plan.item_count)


@router.post("/items/retype", response_model=ItemsRetypeResult)
async def retype_items_route(request: ItemsRetypeRequest, plan: PlanDep) -> ItemsRetypeResult:
    with _edit_errors():
        previous, skipped = retype_items(plan, [(change.id, change.planting_type) for change in request.changes], display_crs(plan.project))
    return ItemsRetypeResult(previous_items=previous, skipped_ids=skipped)


@router.post("/items/move", response_model=ItemsMoveResult)
async def move_items_route(request: ItemsMoveRequest, plan: PlanDep) -> ItemsMoveResult:
    with _edit_errors():
        moved = move_items(
            plan,
            request.ids,
            (request.from_point.x, request.from_point.y),
            (request.to_point.x, request.to_point.y),
            display_crs(plan.project),
        )
    return ItemsMoveResult(items=moved)


@router.post("/items/respace", response_model=ItemsRespaceResult)
async def respace_items_route(request: ItemsRespaceRequest, plan: PlanDep) -> ItemsRespaceResult:
    """«Изменить интервал» у ряда: посадки ряда раскладываются заново с
    `pitch_m` (см. edit_service.respace_items)."""
    with _edit_errors():
        previous, created = respace_items(plan, request.ids, request.pitch_m, display_crs(plan.project))
    return ItemsRespaceResult(previous_items=previous, items=created, item_count=plan.item_count)


@router.get("/items/{item_id}/fixes", response_model=ItemFixesOut)
async def item_fixes_route(item_id: str, plan: PlanDep) -> ItemFixesOut:
    """Как исправить нарушение у одной посадки: сдвиг в допустимую точку, куст
    вместо дерева, удаление — с последствиями (fix_service.item_fixes). Поиск
    точки — реальная геометрия, поэтому в пуле потоков, а не на цикле событий."""
    get_item_or_404(plan, item_id)
    options = await run_in_threadpool(item_fixes, plan, item_id, display_crs(plan.project))
    return ItemFixesOut(item_id=item_id, options=[FixOptionOut(**option) for option in options])


@router.get("/items/{item_id}/nearest-allowed", response_model=NearestAllowedOut)
async def nearest_allowed_route(item_id: str, x: float, y: float, plan: PlanDep) -> NearestAllowedOut:
    """Alt при перетаскивании: ближайшая допустимая точка для посадки, если её
    поставить у (x, y) (координаты отображения). 404 — рядом места нет."""
    get_item_or_404(plan, item_id)
    found = await run_in_threadpool(nearest_allowed, plan, item_id, x, y, display_crs(plan.project))
    if found is None:
        raise HTTPException(status_code=404, detail="Рядом нет места, где нормы позволяют поставить эту посадку.")
    return NearestAllowedOut(**found)


@router.get("/items/{item_id}/allowed-area", response_model=AllowedAreaOut)
async def allowed_area_route(item_id: str, x: float, y: float, plan: PlanDep) -> AllowedAreaOut:
    """Перенос: где вокруг (x, y) (координаты отображения) эту посадку можно
    поставить без нарушений — одним полигоном, чтобы интерфейс закрасил запретное
    место и оставил чистым допустимое. Газон — 404: у него нет «точки»."""
    get_item_or_404(plan, item_id)
    found = await run_in_threadpool(allowed_area, plan, item_id, x, y, display_crs(plan.project))
    if found is None:
        raise HTTPException(status_code=404, detail="Для этой посадки допустимую область не считаем.")
    return AllowedAreaOut(**found)


@router.post("/fixes/apply", response_model=FixesApplyResult)
async def apply_fixes_route(request: FixesApplyRequest, plan: PlanDep) -> FixesApplyResult:
    """«Исправить все так же»: лучший вариант выбранного вида у каждой из `ids`
    (fix_service.apply_fixes). Одним запросом и одним шагом отмены."""
    with _edit_errors():
        outcome = await run_in_threadpool(apply_fixes, plan, request.ids, request.kind, display_crs(plan.project))
    return FixesApplyResult(
        previous_items=outcome.previous, items=outcome.created, skipped_ids=outcome.skipped, item_count=plan.item_count
    )


@router.post("/items/restore", response_model=ItemsRestoreResult)
async def restore_items_route(request: ItemsRestoreRequest, plan: PlanDep) -> ItemsRestoreResult:
    """Undo's counterpart to item deletion -- recreates the exact items in
    `request.items` (a delete's `deleted_items` sent right back)."""
    with _edit_errors():
        restore_items(plan, [f.model_dump() for f in request.items], display_crs(plan.project))
    return ItemsRestoreResult(item_count=plan.item_count)


@router.post("/validate", response_model=ValidateResponse)
async def validate(plan: PlanDep) -> ValidateResponse:
    # Pure shapely computation -- off the event loop the same way
    # generate_plan's geometry work is, so a big plan's worth of checks
    # doesn't stall every other request meanwhile.
    violations = await run_in_threadpool(validate_plan, plan.project, plan)
    return ValidateResponse(violations=[ValidationViolation(**v) for v in violations])


@router.post("/validate/items", response_model=ValidateResponse)
async def validate_items_route(request: ValidateItemsRequest, plan: PlanDep) -> ValidateResponse:
    """Recheck only specific items -- what an edit actually touched -- rather
    than the whole plan (see edit_service.validate_items)."""
    violations, checked_ids = await run_in_threadpool(validate_items_scoped, plan.project, plan, request.ids)
    return ValidateResponse(violations=[ValidationViolation(**v) for v in violations], checked_ids=checked_ids)
