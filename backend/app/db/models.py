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
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, Text
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
    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_new_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    scoring_mode: Mapped[str] = mapped_column(String, default="heuristic")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)

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
    edits: Mapped[list["EditHistory"]] = relationship(back_populates="item", cascade="all, delete-orphan")


class EditHistory(Base):
    __tablename__ = "edit_history"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_new_uuid)
    planting_item_id: Mapped[str] = mapped_column(ForeignKey("planting_items.id"), nullable=False)
    diff: Mapped[dict] = mapped_column(JSONB, default=dict)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    item: Mapped["PlantingItemRow"] = relationship(back_populates="edits")
