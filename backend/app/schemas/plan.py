from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from backend.app.schemas.geo import GeoJSONFeature, GeoJSONFeatureCollection, GeoJSONGeometry

ScoringMode = Literal["heuristic", "ml"]
PlantingType = Literal["tree", "shrub", "lawn"]


class GenerateRequest(BaseModel):
    planting_types: list[PlantingType] = ["tree", "shrub", "lawn"]
    scoring_mode: ScoringMode = "heuristic"
    # None -- the far more common case -- means "use planting_norms.yaml's
    # own tree_default/shrub_default"; bounds are sanity rails (a few
    # centimeters would flood the plan with candidates the way an
    # accidentally-tiny canopy_radius_m once did, see CLAUDE.md), not a
    # claim about what's landscaping-correct.
    tree_spacing_m: float | None = Field(default=None, ge=0.5, le=15.0)
    shrub_spacing_m: float | None = Field(default=None, ge=0.3, le=10.0)


class GenerateJobOut(BaseModel):
    """POST /generate starts a background job instead of blocking the
    request for however long candidate generation + placement takes on a
    real-scale territory -- see backend/app/services/generation_jobs.py."""

    job_id: str


GenerateJobState = Literal["pending", "done", "error"]


class GenerateJobStatus(BaseModel):
    status: GenerateJobState
    plan_id: str | None = None
    error: str | None = None
    # Best-effort, same shape as ProjectUploadJobStatus -- see
    # generation_jobs.py's own docstring. `stage` names the planting_type
    # that most recently finished; `progress` is types-done/types-total.
    stage: str | None = None
    progress: float | None = None


class ExportJobOut(BaseModel):
    """POST /export-dxf starts a background job instead of blocking the
    request -- writing a real-scale plan (hundreds of thousands of items)
    measured up to ~3 minutes (see CLAUDE.md's export benchmark), the same
    class of problem /generate already solves this way. See
    backend/app/services/export_jobs.py."""

    job_id: str


ExportJobState = Literal["pending", "done", "error"]


class ExportJobStatus(BaseModel):
    status: ExportJobState
    error: str | None = None


class PlanOut(BaseModel):
    plan_id: str
    scoring_mode: ScoringMode
    features: GeoJSONFeatureCollection


class PlanSummary(BaseModel):
    """One row in the project's plan history — lets the UI show every past
    generation (heuristic vs ml, one run vs another) instead of only ever
    tracking whichever plan was generated most recently, which is what made
    separate generations look like they were "getting mixed up" together.
    """

    plan_id: str
    scoring_mode: ScoringMode
    created_at: datetime
    is_current: bool
    item_count: int


class ItemPatch(BaseModel):
    """Graphical edit: move/retype/restyle one planting item. Omit a field to leave it unchanged."""

    geometry: GeoJSONGeometry | None = None
    planting_type: PlantingType | None = None
    species: str | None = None


class ItemIdsRequest(BaseModel):
    ids: list[str]


class ItemsDeleteResult(BaseModel):
    """Pre-delete snapshots, for undo via POST .../items/restore. `item_count`
    is the plan's fresh total -- lets the frontend update the plan-history
    sidebar's count for this plan without a separate GET .../plans call."""

    deleted_items: list[GeoJSONFeature]
    item_count: int


class ItemTypeChange(BaseModel):
    id: str
    planting_type: PlantingType


class ItemsRetypeRequest(BaseModel):
    """One target type per item -- a selection sends the same type for all,
    an undo sends each item's own previous type back."""

    changes: list[ItemTypeChange]


class ItemsRetypeResult(BaseModel):
    """`previous_items`: pre-change snapshots of what actually changed (for
    undo) -- geometry never changes here, so the frontend patches its local
    copy's `planting_type` directly rather than needing a post-change
    snapshot back. `skipped_ids`: items whose geometry can't take the
    requested type (e.g. a lawn polygon asked to become a tree), left
    untouched."""

    previous_items: list[GeoJSONFeature]
    skipped_ids: list[str]


class LngLat(BaseModel):
    x: float
    y: float


class ItemsMoveRequest(BaseModel):
    """Translate these items by the vector from `from_point` to `to_point`
    (the drag's start and end, WGS84 lng/lat)."""

    ids: list[str]
    from_point: LngLat
    to_point: LngLat


class ItemsMoveResult(BaseModel):
    """Post-move snapshots of the moved items -- the exact stored position
    depends on a WGS84 round trip through the project's source_crs, which the
    frontend's own optimistic drag preview (plain WGS84 math) doesn't
    replicate exactly, so it takes these back to patch its local copy rather
    than assuming its preview already matches what got stored."""

    items: list[GeoJSONFeature]


class ItemsRestoreRequest(BaseModel):
    """Undo's counterpart to a delete: recreate these exact items (same id,
    geometry, type, species, score, rationale) -- a delete's own
    `deleted_items` sent right back.
    """

    items: list[GeoJSONFeature]


class ItemsRestoreResult(BaseModel):
    """`item_count`: the plan's fresh total, same reasoning as
    ItemsDeleteResult. Nothing else -- the caller already has the exact
    features it just asked to restore."""

    item_count: int


class ValidationViolation(BaseModel):
    item_id: str
    message: str


class ValidateResponse(BaseModel):
    violations: list[ValidationViolation]


class ValidateItemsRequest(BaseModel):
    """Recheck only these items against the setback rulebook -- what an edit
    actually touched -- instead of the whole plan (see
    edit_service.validate_items)."""

    ids: list[str]
