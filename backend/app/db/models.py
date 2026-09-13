"""SQLAlchemy + GeoAlchemy2 models.

Geometry columns use srid=0 (no PostGIS-enforced SRID/reprojection): each
project's geometries live in whatever local metric CRS that project's source
data came in (see geo_engine/crs.py), tracked as free text in
`Project.source_crs`, and are only reprojected to WGS84 in application code
at the API boundary. This is deliberate — the real source CRS is unknown
until 2026-09-15, so we don't want PostGIS silently assuming one.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from geoalchemy2 import Geometry
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    source_crs: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    layers: Mapped[list["Layer"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    plans: Mapped[list["Plan"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class Layer(Base):
    """One imported geo object (utility line, building, zoning polygon, ...)."""

    __tablename__ = "layers"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_new_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)  # "utility" | "zone"
    object_type: Mapped[str] = mapped_column(String, nullable=False)
    geometry: Mapped[str] = mapped_column(Geometry(geometry_type="GEOMETRY", srid=0), nullable=False)
    attrs: Mapped[dict] = mapped_column(JSONB, default=dict)

    project: Mapped["Project"] = relationship(back_populates="layers")


class Plan(Base):
    """A `Plan` row is the *recipe* that produced a plan (scoring_mode +
    planting_types), not necessarily its materialized `planting_items` rows.
    `generate_plan` is a pure function of (project layers, norms, model
    artifact, this recipe) -- see CLAUDE.md -- so a plan that nobody has
    hand-edited is fully reproducible and doesn't need its (potentially
    hundreds of thousands of) item rows kept at rest forever. `materialized`
    tracks whether they currently exist; `pipeline_service.ensure_materialized`
    recomputes them on demand when a pruned plan is opened again.
    `has_manual_edits` plans are never pruned -- a human-authored change isn't
    derivable from the recipe, so it has to stay real data.
    """

    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_new_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    scoring_mode: Mapped[str] = mapped_column(String, default="heuristic")
    planting_types: Mapped[list[str]] = mapped_column(JSONB, default=list)
    # Per-generation tree/shrub spacing override (meters) -- None (not 0 or a
    # fallback constant) means "use whatever planting_norms.yaml says",
    # genuinely distinct from "the user chose a value", unlike scoring_mode/
    # planting_types above which are always meaningfully set. Part of the
    # recipe for the same reason those are: pipeline_service.ensure_materialized
    # reads these back to recompute a collapsed plan identically to how it
    # was first generated, not with today's YAML default.
    tree_spacing_m: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    shrub_spacing_m: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    materialized: Mapped[bool] = mapped_column(Boolean, default=True)
    has_manual_edits: Mapped[bool] = mapped_column(Boolean, default=False)

    project: Mapped["Project"] = relationship(back_populates="plans")
    items: Mapped[list["PlantingItemRow"]] = relationship(back_populates="plan", cascade="all, delete-orphan")


class PlantingItemRow(Base):
    __tablename__ = "planting_items"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_new_uuid)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"), nullable=False)
    geometry: Mapped[str] = mapped_column(Geometry(geometry_type="GEOMETRY", srid=0), nullable=False)
    planting_type: Mapped[str] = mapped_column(String, nullable=False)
    species: Mapped[str] = mapped_column(String, default="default")
    score: Mapped[float] = mapped_column(Float, default=0.0)
    rationale: Mapped[str] = mapped_column(Text, default="")
    is_manual_edit: Mapped[bool] = mapped_column(Boolean, default=False)

    plan: Mapped["Plan"] = relationship(back_populates="items")
