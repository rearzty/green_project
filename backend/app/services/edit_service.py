"""Manual plan corrections: point-and-drag item patches, the fixed-vocabulary
structured "text" edits, and post-edit normative validation.
"""

from __future__ import annotations

from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings
from backend.app.db.models import EditHistory, Plan, PlantingItemRow, Project
from backend.app.services.geo_io import db_to_shape, from_wgs84, layers_to_domain, shape_to_db
from geo_engine.buffers import build_exclusion_zone
from geo_engine.norms import load_norms

_POINT_PLANTING_TYPES = {"tree", "shrub"}
_POLYGON_PLANTING_TYPES = {"lawn"}


class UnknownOperationError(ValueError):
    pass


class PlantingTypeGeometryMismatchError(ValueError):
    """A planting_type change would pair the type with a geometry kind
    nothing downstream produces for it: geo_engine.candidates only ever makes
    lawn a whole sub-polygon (generate_area_candidates) and tree/shrub a
    point (generate_point_candidates), and ml_scoring/the DXF export assume
    that pairing holds.
    """


def _assert_planting_type_matches_geometry(planting_type: str, geometry: BaseGeometry) -> None:
    geom_type = geometry.geom_type
    if planting_type in _POINT_PLANTING_TYPES and geom_type != "Point":
        raise PlantingTypeGeometryMismatchError(
            f"planting_type='{planting_type}' requires Point geometry, got {geom_type}"
        )
    if planting_type in _POLYGON_PLANTING_TYPES and geom_type != "Polygon":
        raise PlantingTypeGeometryMismatchError(
            f"planting_type='{planting_type}' requires Polygon geometry, got {geom_type}"
        )


async def apply_item_patch(
    session: AsyncSession,
    item: PlantingItemRow,
    geometry: dict | None,
    planting_type: str | None,
    species: str | None,
    source_crs: str | None,
) -> PlantingItemRow:
    # The frontend drags markers in WGS84 (Leaflet's native CRS) -- every
    # other stored geometry is in the project's source_crs, so reproject back
    # before storing/validating or this silently drifts off everything else.
    new_geometry = from_wgs84(shape(geometry), source_crs) if geometry is not None else None
    if geometry is not None or planting_type is not None:
        _assert_planting_type_matches_geometry(
            planting_type if planting_type is not None else item.planting_type,
            new_geometry if new_geometry is not None else db_to_shape(item.geometry),
        )

    diff: dict = {}
    if geometry is not None:
        diff["geometry"] = geometry
        item.geometry = shape_to_db(new_geometry)
    if planting_type is not None:
        diff["planting_type"] = planting_type
        item.planting_type = planting_type
    if species is not None:
        diff["species"] = species
        item.species = species

    item.is_manual_edit = True
    item.plan.has_manual_edits = True  # a hand-edited plan is no longer just a recipe -- see Plan's docstring, never pruned by _prune_stale_plans
    session.add(EditHistory(planting_item_id=item.id, diff=diff))
    await session.commit()
    await session.refresh(item)
    return item


async def delete_item(session: AsyncSession, item: PlantingItemRow) -> None:
    """Remove a single planting item by id -- the point-and-click counterpart
    to the area-based `remove_within_radius`/`exclude_polygon` structured
    edits, for deleting exactly one tree/shrub a designer clicked on. No
    EditHistory row for this one (same as the other two delete operations,
    above) -- it would reference a planting_item_id that no longer exists the
    moment this commits, which is a contradiction, not an audit trail.
    """
    plan = item.plan
    await session.delete(item)
    plan.item_count -= 1
    plan.has_manual_edits = True  # see Plan's docstring: never pruned by _prune_stale_plans once a human has touched it
    await session.commit()


async def apply_structured_edit(session: AsyncSession, plan: Plan, operation: str, params: dict, source_crs: str | None) -> None:
    """`params`' coordinates (x/y, polygon) arrive in WGS84 — same reasoning
    as apply_item_patch's geometry reprojection, since these come from
    clicks/drawing on the Leaflet map, not from the project's own CRS."""
    if operation == "remove_within_radius":
        center = from_wgs84(Point(params["x"], params["y"]), source_crs)
        radius = float(params["radius_m"])
        for item in list(plan.items):
            if db_to_shape(item.geometry).distance(center) <= radius:
                await session.delete(item)
                plan.item_count -= 1

    elif operation == "replace_type_in_zone":
        zone_geom = from_wgs84(shape(params["polygon"]), source_crs)
        new_type = params["planting_type"]
        matching_items = [item for item in plan.items if db_to_shape(item.geometry).within(zone_geom)]
        for item in matching_items:
            _assert_planting_type_matches_geometry(new_type, db_to_shape(item.geometry))
        for item in matching_items:
            item.planting_type = new_type
            item.is_manual_edit = True
            session.add(EditHistory(planting_item_id=item.id, diff={"operation": operation, "params": params}))

    elif operation == "exclude_polygon":
        zone_geom = from_wgs84(shape(params["polygon"]), source_crs)
        for item in list(plan.items):
            if db_to_shape(item.geometry).intersects(zone_geom):
                await session.delete(item)
                plan.item_count -= 1

    else:
        raise UnknownOperationError(f"Unknown structured edit operation: {operation}")

    plan.has_manual_edits = True  # see Plan's docstring: this plan now holds real, non-derivable data -- never pruned
    await session.commit()


_VIOLATION_AREA_TOLERANCE_M2 = 1e-6


def _violates_setback(geometry, exclusion) -> bool:
    """A Point (tree/shrub) either is or isn't inside the exclusion buffer,
    so `intersects` is the right check there. A Polygon (lawn) footprint is
    carved out as exactly `territory.difference(exclusion_zone)`
    (buffers.buildable_area) though, so its boundary always touches the
    exclusion zone's boundary by construction -- `intersects` would flag
    essentially every lawn item as a false positive. Compare the actual
    overlapping area instead, which ignores boundary-only contact.
    """
    if geometry.geom_type == "Point":
        return geometry.intersects(exclusion)
    return geometry.intersection(exclusion).area > _VIOLATION_AREA_TOLERANCE_M2


def validate_plan(project: Project, plan: Plan) -> list[dict]:
    """Re-check every item in the plan against the setback rulebook — used
    after manual edits, which are allowed to violate norms (a designer may
    have a legitimate reason) but must surface a clear warning when they do.
    """
    norms = load_norms(settings.planting_norms_path)
    utilities, zones = layers_to_domain(project.layers)

    violations: list[dict] = []
    planting_types = {item.planting_type for item in plan.items}

    for planting_type in planting_types:
        exclusion = build_exclusion_zone(utilities, zones, planting_type, norms)
        if exclusion is None or exclusion.is_empty:
            continue
        for item in plan.items:
            if item.planting_type != planting_type:
                continue
            geometry = db_to_shape(item.geometry)
            if _violates_setback(geometry, exclusion):
                violations.append(
                    {
                        "item_id": item.id,
                        "message": f"Нарушен норматив отступа для типа посадки '{planting_type}'.",
                    }
                )

    return violations
