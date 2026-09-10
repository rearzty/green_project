"""Manual plan corrections: point-and-drag item patches, the fixed-vocabulary
structured "text" edits, and post-edit normative validation.
"""

from __future__ import annotations

from shapely.geometry import Point, shape
from sqlalchemy.orm import Session

from backend.app.core.config import settings
from backend.app.db.models import EditHistory, Plan, PlantingItemRow, Project
from backend.app.services.geo_io import db_to_shape, layers_to_domain, shape_to_db
from geo_engine.buffers import build_exclusion_zone
from geo_engine.norms import load_norms


class UnknownOperationError(ValueError):
    pass


def apply_item_patch(session: Session, item: PlantingItemRow, geometry: dict | None, planting_type: str | None, species: str | None) -> PlantingItemRow:
    diff: dict = {}
    if geometry is not None:
        # TODO(post-15.09): the frontend drags markers in WGS84 (Leaflet's
        # native CRS), but every other stored geometry is in the project's
        # source_crs — for a project with a real source_crs set, this needs
        # a WGS84 -> source_crs reprojection symmetric to geo_io._to_wgs84,
        # or graphical edits will silently drift off the other geometries.
        # A no-op today since no project has source_crs set yet.
        diff["geometry"] = geometry
        item.geometry = shape_to_db(shape(geometry))
    if planting_type is not None:
        diff["planting_type"] = planting_type
        item.planting_type = planting_type
    if species is not None:
        diff["species"] = species
        item.species = species

    item.is_manual_edit = True
    session.add(EditHistory(planting_item_id=item.id, diff=diff))
    session.commit()
    session.refresh(item)
    return item


def apply_structured_edit(session: Session, plan: Plan, operation: str, params: dict) -> None:
    if operation == "remove_within_radius":
        center = Point(params["x"], params["y"])
        radius = float(params["radius_m"])
        for item in list(plan.items):
            if db_to_shape(item.geometry).distance(center) <= radius:
                session.delete(item)

    elif operation == "replace_type_in_zone":
        zone_geom = shape(params["polygon"])
        new_type = params["planting_type"]
        for item in plan.items:
            if db_to_shape(item.geometry).within(zone_geom):
                item.planting_type = new_type
                item.is_manual_edit = True
                session.add(EditHistory(planting_item_id=item.id, diff={"operation": operation, "params": params}))

    elif operation == "exclude_polygon":
        zone_geom = shape(params["polygon"])
        for item in list(plan.items):
            if db_to_shape(item.geometry).intersects(zone_geom):
                session.delete(item)

    else:
        raise UnknownOperationError(f"Unknown structured edit operation: {operation}")

    session.commit()


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
            if geometry.intersects(exclusion):
                violations.append(
                    {
                        "item_id": item.id,
                        "message": f"Нарушен норматив отступа для типа посадки '{planting_type}'.",
                    }
                )

    return violations
