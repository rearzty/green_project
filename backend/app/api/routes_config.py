from __future__ import annotations

from fastapi import APIRouter

from backend.app.core.config import settings
from geo_engine.norms import load_norms

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("/planting-norms")
def get_planting_norms() -> dict:
    """Exposes the same rulebook geo_engine uses internally, so the frontend
    can render the legend and do client-side sanity checks without
    duplicating the setback values.
    """
    return load_norms(settings.planting_norms_path).model_dump()
