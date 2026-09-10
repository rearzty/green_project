from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from backend.app.schemas.geo import GeoJSONFeatureCollection, GeoJSONGeometry

ScoringMode = Literal["heuristic", "ml"]
PlantingType = Literal["tree", "shrub", "lawn"]


class GenerateRequest(BaseModel):
    planting_types: list[PlantingType] = ["tree", "shrub", "lawn"]
    scoring_mode: ScoringMode = "heuristic"


class PlanOut(BaseModel):
    plan_id: str
    scoring_mode: ScoringMode
    features: GeoJSONFeatureCollection


class ItemPatch(BaseModel):
    """Graphical edit: move/retype/restyle one planting item. Omit a field to leave it unchanged."""

    geometry: GeoJSONGeometry | None = None
    planting_type: PlantingType | None = None
    species: str | None = None


class StructuredEditRequest(BaseModel):
    """A parameterized editing command — the MVP's "text correction" is a
    fixed vocabulary of operations rather than free-text/NL, so it stays
    predictable and doesn't need an LLM in the critical path.
    """

    operation: Literal["remove_within_radius", "replace_type_in_zone", "exclude_polygon"]
    params: dict


class ValidationViolation(BaseModel):
    item_id: str
    message: str


class ValidateResponse(BaseModel):
    violations: list[ValidationViolation]
