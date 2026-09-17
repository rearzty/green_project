"""In-memory domain records.

No database -- removed by explicit user decision (see docs/decision_log.md
for the "why": the app is meant to be upload -> work in one session -> forget,
not a system that remembers projects forever). These are plain dataclasses,
not SQLAlchemy models, but keep the exact names and fields the rest of the
backend (and every existing test -- none of them ever touched a real DB
either, see e.g. test_compliance_service.py's own docstring) already imports
from `backend.app.db.models`, so this is a storage-layer swap, not a change
to the domain shape. `store.py` is where instances of these actually live.

`geometry` fields hold a plain shapely object directly -- there is no DB
round trip to serialize for anymore, so the old GeoAlchemy2 WKBElement
wrapping (`geo_io.shape_to_db`/`db_to_shape`) is gone too; those two
functions are kept as identity passthroughs so the many call sites built
around "wrap before storing, unwrap before using" don't all need editing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(kw_only=True)
class Layer:
    """One imported geo object (utility line, building, zoning polygon, ...)."""

    id: str = field(default_factory=_new_uuid)
    project_id: str
    kind: str  # "utility" | "zone"
    object_type: str
    geometry: Any  # a shapely geometry
    attrs: dict = field(default_factory=dict)


@dataclass(kw_only=True)
class PlantingItemRow:
    id: str = field(default_factory=_new_uuid)
    plan_id: str
    geometry: Any
    planting_type: str
    species: str = "default"
    score: float = 0.0
    rationale: str = ""
    is_manual_edit: bool = False
    # Back-reference to the owning Plan -- ORM's `relationship(back_populates=...)`
    # had no dataclass equivalent, so whatever appends an item to `plan.items`
    # is responsible for setting this too (see store.py::attach_items).
    plan: "Plan | None" = None


@dataclass(kw_only=True)
class Plan:
    """A `Plan` is the *recipe* that produced a plan (scoring_mode +
    planting_types), not necessarily its materialized `items`. `generate_plan`
    is a pure function of (project layers, norms, model artifact, this
    recipe) -- see CLAUDE.md -- so a plan that nobody has hand-edited is fully
    reproducible and doesn't need its (potentially hundreds of thousands of)
    items kept around. `materialized` tracks whether they currently exist;
    `pipeline_service.ensure_materialized` recomputes them on demand when a
    pruned plan is opened again. `has_manual_edits` plans are never pruned --
    a human-authored change isn't derivable from the recipe.
    """

    id: str = field(default_factory=_new_uuid)
    project_id: str
    scoring_mode: str = "heuristic"
    planting_types: list[str] = field(default_factory=list)
    # Per-generation tree/shrub spacing override (meters) -- None means "use
    # whatever planting_norms.yaml says", distinct from "the user chose a
    # value". pipeline_service.ensure_materialized reads these back to
    # recompute a collapsed plan identically to how it was first generated.
    tree_spacing_m: float | None = None
    shrub_spacing_m: float | None = None
    created_at: datetime = field(default_factory=_utcnow)
    is_current: bool = True
    item_count: int = 0
    materialized: bool = True
    has_manual_edits: bool = False

    project: "Project | None" = None
    items: list[PlantingItemRow] = field(default_factory=list)


@dataclass(kw_only=True)
class Project:
    id: str = field(default_factory=_new_uuid)
    name: str = ""
    source_crs: str | None = None
    created_at: datetime = field(default_factory=_utcnow)

    layers: list[Layer] = field(default_factory=list)
    plans: list[Plan] = field(default_factory=list)
