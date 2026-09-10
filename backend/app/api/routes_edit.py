from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.app.api.deps import get_item_or_404, get_plan_or_404, get_project_or_404
from backend.app.db.models import Plan, Project
from backend.app.db.session import get_session
from backend.app.schemas.geo import GeoJSONFeature
from backend.app.schemas.plan import ItemPatch, StructuredEditRequest, ValidateResponse, ValidationViolation
from backend.app.services.edit_service import UnknownOperationError, apply_item_patch, apply_structured_edit, validate_plan
from backend.app.services.geo_io import planting_item_to_geojson_feature

router = APIRouter(prefix="/api/projects/{project_id}/plans/{plan_id}", tags=["edit"])


@router.patch("/items/{item_id}", response_model=GeoJSONFeature)
def patch_item(
    item_id: str,
    patch: ItemPatch,
    plan: Plan = Depends(get_plan_or_404),
    session: Session = Depends(get_session),
) -> GeoJSONFeature:
    item = get_item_or_404(plan, item_id)
    geometry_dict = patch.geometry.model_dump() if patch.geometry is not None else None
    updated = apply_item_patch(session, item, geometry_dict, patch.planting_type, patch.species)
    return planting_item_to_geojson_feature(updated, plan.project.source_crs)


@router.post("/edit-structured", status_code=204)
def edit_structured(
    request: StructuredEditRequest,
    plan: Plan = Depends(get_plan_or_404),
    session: Session = Depends(get_session),
) -> None:
    try:
        apply_structured_edit(session, plan, request.operation, request.params)
    except UnknownOperationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=f"Missing required param: {exc}") from exc


@router.post("/validate", response_model=ValidateResponse)
def validate(
    project: Project = Depends(get_project_or_404),
    plan: Plan = Depends(get_plan_or_404),
) -> ValidateResponse:
    violations = validate_plan(project, plan)
    return ValidateResponse(violations=[ValidationViolation(**v) for v in violations])
