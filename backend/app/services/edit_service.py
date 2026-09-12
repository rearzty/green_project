"""Manual plan corrections: single-item patches, id-based batch edits (what
the map's selection tool and the frontend's local undo/redo drive), and
normative validation.

User-facing error messages here are Russian on purpose -- the frontend shows
them to the designer as-is in its toast notifications.
"""

from __future__ import annotations

import numpy as np
import shapely
from shapely.affinity import translate
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry
from sqlalchemy import delete, insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models import Plan, PlantingItemRow, Project
from backend.app.services import exclusion_cache
from backend.app.services.geo_io import db_to_shape, from_wgs84, planting_items_to_geojson_dicts, shape_to_db

_POINT_PLANTING_TYPES = {"tree", "shrub"}
_POLYGON_PLANTING_TYPES = {"lawn"}
_PLANTING_TYPE_LABELS = {"tree": "Дерево", "shrub": "Кустарник", "lawn": "Газон"}

# asyncpg caps one statement at 32767 bind parameters -- keep IN (...) lists well under that.
_IN_CLAUSE_CHUNK = 5000


class PlantingTypeGeometryMismatchError(ValueError):
    """A planting_type change would pair the type with a geometry kind
    nothing downstream produces for it: geo_engine.candidates only ever makes
    lawn a whole sub-polygon (generate_area_candidates) and tree/shrub a
    point (generate_point_candidates), and ml_scoring/the DXF export assume
    that pairing holds.
    """


class OutOfTerritoryError(ValueError):
    """A manual edit tried to place an item outside the project's own
    territory boundary -- physically impossible (you can't plant a tree
    beyond the edge of the plot), unlike a setback violation, which is a
    design choice /validate warns about but manual edits are still allowed
    to make (see validate_plan's docstring).
    """


class ItemsNotFoundError(LookupError):
    """Some ids in a batch edit no longer exist in the plan -- typically a
    stale selection or undo entry after the plan changed underneath it.
    Raised before anything is mutated, so a batch is all-or-nothing."""


class ItemsAlreadyExistError(LookupError):
    """A restore was asked to recreate ids that are already in the plan (e.g.
    an undo applied twice). Refused instead of hitting a primary-key clash."""


def _assert_planting_type_matches_geometry(planting_type: str, geometry: BaseGeometry) -> None:
    geom_type = geometry.geom_type
    if planting_type in _POINT_PLANTING_TYPES and geom_type != "Point":
        label = _PLANTING_TYPE_LABELS[planting_type]
        raise PlantingTypeGeometryMismatchError(f"Тип «{label}» можно назначить только отдельной посадке, а не площадному объекту.")
    if planting_type in _POLYGON_PLANTING_TYPES and geom_type != "Polygon":
        raise PlantingTypeGeometryMismatchError("Газон — площадной объект: его нельзя назначить отдельному дереву или кусту.")


_TERRITORY_TOLERANCE_M = 1e-3  # 1mm -- see _assert_all_within_territory


def _assert_all_within_territory(geometries: list[BaseGeometry], project: Project) -> None:
    """One cached territory lookup and one vectorized `within` for a whole
    batch -- every manual edit calls this, so re-parsing project layers each
    time (like this used to) is O(edits x layers) over a session; the
    territory itself never changes after upload, so exclusion_cache keeps it
    built once per project (see its module docstring).

    Checked against the territory buffered outward by _TERRITORY_TOLERANCE_M,
    not the exact boundary -- restoring a deleted item reprojects its
    delete-snapshot geometry source_crs -> WGS84 -> source_crs (see
    delete_items/restore_items), a real forward+inverse pyproj round trip
    whenever source_crs is set, not a no-op. Measured noise from that round
    trip: ~1e-9 m per coordinate. A lawn polygon carved out by
    buffers.buildable_area() as exactly territory.difference(exclusion)
    always has some edge touching the territory boundary by construction, and
    plain shapely.within has zero tolerance for a vertex nudged even a
    billionth of a meter past it -- restoring an item that never actually
    moved could still be rejected as "outside the territory". Same class of
    problem violation_mask already tolerates for the exclusion-zone check
    (area-based there since it also has to ignore genuine boundary contact,
    not just noise); a 1mm buffer here is simpler and works for both Point
    and Polygon geometries uniformly, while staying far below any real edit
    distance (canopy radii/min_distance are metre-scale) so a genuinely
    out-of-territory placement is still rejected.
    """
    if not geometries:
        return

    def _all_within_tolerant(territory: BaseGeometry) -> bool:
        return bool(np.all(shapely.within(geometries, territory.buffer(_TERRITORY_TOLERANCE_M))))

    within = exclusion_cache.with_territory(project, _all_within_tolerant)
    if not within:
        raise OutOfTerritoryError("Нельзя разместить посадку за границей участка.")


def _items_by_ids(plan: Plan, ids: list[str]) -> list[PlantingItemRow]:
    by_id = {item.id: item for item in plan.items}
    unique_ids = list(dict.fromkeys(ids))
    missing = [item_id for item_id in unique_ids if item_id not in by_id]
    if missing:
        raise ItemsNotFoundError(f"Не найдено объектов плана: {len(missing)} — план изменился. Выделите объекты заново.")
    return [by_id[item_id] for item_id in unique_ids]


def _chunks(values: list, size: int = _IN_CLAUSE_CHUNK):
    for start in range(0, len(values), size):
        yield values[start : start + size]


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
    if new_geometry is not None:
        _assert_all_within_territory([new_geometry], item.plan.project)

    if geometry is not None:
        item.geometry = shape_to_db(new_geometry)
    if planting_type is not None:
        item.planting_type = planting_type
    if species is not None:
        item.species = species

    item.is_manual_edit = True
    item.plan.has_manual_edits = True  # a hand-edited plan is no longer just a recipe -- see Plan's docstring, never pruned by _prune_stale_plans
    await session.commit()
    await session.refresh(item)
    return item


async def delete_item(session: AsyncSession, item: PlantingItemRow) -> None:
    """Remove a single planting item by id."""
    plan = item.plan
    await session.delete(item)
    plan.item_count -= 1
    plan.has_manual_edits = True  # see Plan's docstring: never pruned by _prune_stale_plans once a human has touched it
    await session.commit()


async def delete_items(session: AsyncSession, plan: Plan, ids: list[str], source_crs: str | None) -> list[dict]:
    """Batch delete by id (the selection tool's Del, the redo of a delete).
    Returns each item's pre-delete GeoJSON snapshot -- exactly what
    restore_items needs to undo it. Core DELETEs rather than per-object ORM
    deletes: the ORM's own flush would emit one DELETE statement per row,
    which a large selection can't afford.
    """
    items = _items_by_ids(plan, ids)
    snapshots = planting_items_to_geojson_dicts(items, source_crs)
    for chunk in _chunks([item.id for item in items]):
        await session.execute(delete(PlantingItemRow).where(PlantingItemRow.id.in_(chunk)).execution_options(synchronize_session=False))
    plan.item_count -= len(items)
    plan.has_manual_edits = True
    await session.commit()
    return snapshots


async def retype_items(
    session: AsyncSession, plan: Plan, changes: list[tuple[str, str]], source_crs: str | None
) -> tuple[list[dict], list[str]]:
    """Batch planting_type change, one target type per item -- so the same
    call serves the selection tool ("make all of these trees") and an undo
    restoring each item's own previous type.

    Items whose geometry can't take the requested type (a lawn polygon asked
    to become a tree) are skipped and reported, not treated as a failure: for
    a selection that happens to include the lawn, "retype the points, leave
    the lawn alone" is what the designer means. Items already of the
    requested type are left alone and not reported as changed, so their undo
    won't touch them either. Returns (pre-change snapshots of what actually
    changed, skipped ids) -- geometry never changes here, so the frontend
    patches its local copy's `planting_type` directly instead of needing a
    post-change snapshot back.
    """
    items = _items_by_ids(plan, [item_id for item_id, _ in changes])
    target_by_id = dict(changes)

    changed: list[tuple[PlantingItemRow, str]] = []
    skipped: list[str] = []
    for item in items:
        new_type = target_by_id[item.id]
        try:
            _assert_planting_type_matches_geometry(new_type, db_to_shape(item.geometry))
        except PlantingTypeGeometryMismatchError:
            skipped.append(item.id)
            continue
        if item.planting_type != new_type:
            changed.append((item, new_type))

    previous = planting_items_to_geojson_dicts([item for item, _ in changed], source_crs)
    for item, new_type in changed:
        item.planting_type = new_type
        item.is_manual_edit = True
    if changed:
        plan.has_manual_edits = True
    await session.commit()
    return previous, skipped


async def move_items(
    session: AsyncSession,
    plan: Plan,
    ids: list[str],
    from_point: tuple[float, float],
    to_point: tuple[float, float],
    source_crs: str | None,
) -> list[dict]:
    """Batch translate by id -- the selection tool's drag, a single marker's
    drag, and both directions of their undo/redo. `from_point`/`to_point` are
    the drag's start and end (WGS84, like every other edit); the vector is
    their difference once reprojected into source_crs, applied to each item's
    own geometry, so a point and a lawn polygon move the same way. Id-based
    rather than zone-based on purpose: re-selecting "whatever is in this
    rectangle now" at undo time picked up the wrong items once they'd moved.
    All-or-nothing -- if any moved item would leave the territory, nothing
    moves. Returns the moved items' post-move GeoJSON snapshots: the exact
    stored position depends on a WGS84 round trip through source_crs, which
    the frontend's own optimistic drag preview (plain WGS84 math) doesn't
    replicate exactly, so it takes these back rather than assuming its
    preview already matches what got stored.
    """
    items = _items_by_ids(plan, ids)
    start = from_wgs84(Point(*from_point), source_crs)
    end = from_wgs84(Point(*to_point), source_crs)
    dx, dy = end.x - start.x, end.y - start.y

    moved = [translate(db_to_shape(item.geometry), xoff=dx, yoff=dy) for item in items]
    _assert_all_within_territory(moved, plan.project)
    for item, geometry in zip(items, moved):
        item.geometry = shape_to_db(geometry)
        item.is_manual_edit = True
    if items:
        plan.has_manual_edits = True
    await session.commit()
    return planting_items_to_geojson_dicts(items, source_crs)


async def restore_items(session: AsyncSession, plan: Plan, features: list[dict], source_crs: str | None) -> None:
    """Undo's counterpart to a delete -- recreates specific items (same id,
    geometry, type, species, score, rationale) from their pre-delete GeoJSON
    snapshots. Validated like a fresh edit (type/geometry match, inside the
    territory) even though both should hold for something that existed a
    moment ago -- cheap, and avoids trusting client-supplied data unchecked.
    Doesn't return anything: the caller already has these exact features (it
    sent them), so there's nothing new to hand back.
    """
    existing_ids = {item.id for item in plan.items}
    if any(feature["properties"]["id"] in existing_ids for feature in features):
        raise ItemsAlreadyExistError("Эти объекты уже есть в плане — отменять нечего.")

    geometries = [from_wgs84(shape(feature["geometry"]), source_crs) for feature in features]
    for feature, geometry in zip(features, geometries):
        _assert_planting_type_matches_geometry(feature["properties"]["planting_type"], geometry)
    _assert_all_within_territory(geometries, plan.project)

    rows = [
        {
            "id": feature["properties"]["id"],
            "plan_id": plan.id,
            "geometry": shape_to_db(geometry),
            "planting_type": feature["properties"]["planting_type"],
            "species": feature["properties"].get("species", "default"),
            "score": feature["properties"].get("score", 0.0),
            "rationale": feature["properties"].get("rationale", ""),
            # Undo puts the item back exactly as it was -- a generated item stays "generated".
            "is_manual_edit": bool(feature["properties"].get("is_manual_edit", False)),
        }
        for feature, geometry in zip(features, geometries)
    ]
    if rows:
        await session.execute(insert(PlantingItemRow), rows)
    plan.item_count += len(rows)
    plan.has_manual_edits = True
    await session.commit()


_VIOLATION_AREA_TOLERANCE_M2 = 1e-6


def violation_mask(geometries: list[BaseGeometry], exclusion: BaseGeometry | None) -> np.ndarray:
    """Vectorized setback check -- True where an item violates `exclusion`.

    A Point (tree/shrub) violates by being inside the buffer at all
    (`intersects`). A Polygon (lawn) is carved out as exactly
    territory.difference(exclusion) by buffers.buildable_area, so its
    boundary always touches the exclusion boundary by construction; compare
    the actual overlapping area instead, so boundary-only contact isn't
    flagged (it used to be -- every generated lawn was a false positive).
    """
    geoms = np.empty(len(geometries), dtype=object)
    geoms[:] = geometries
    mask = np.zeros(len(geoms), dtype=bool)
    if exclusion is None or exclusion.is_empty or len(geoms) == 0:
        return mask

    is_point = shapely.get_type_id(geoms) == shapely.GeometryType.POINT
    if is_point.any():
        mask[is_point] = shapely.intersects(geoms[is_point], exclusion)
    if (~is_point).any():
        mask[~is_point] = shapely.area(shapely.intersection(geoms[~is_point], exclusion)) > _VIOLATION_AREA_TOLERANCE_M2
    return mask


def _check_violations(project: Project, items: list[PlantingItemRow]) -> list[dict]:
    by_type: dict[str, list[PlantingItemRow]] = {}
    for item in items:
        by_type.setdefault(item.planting_type, []).append(item)

    violations: list[dict] = []
    for planting_type, type_items in by_type.items():
        geometries = [db_to_shape(item.geometry) for item in type_items]
        mask = exclusion_cache.with_exclusion_zone(project, planting_type, lambda zone: violation_mask(geometries, zone))
        label = _PLANTING_TYPE_LABELS.get(planting_type, planting_type)
        violations.extend(
            {"item_id": item.id, "message": f"{label}: нарушен норматив отступа от сетей или зданий."}
            for item, violated in zip(type_items, mask)
            if violated
        )
    return violations


def validate_plan(project: Project, plan: Plan) -> list[dict]:
    """Re-check every item in the plan against the setback rulebook. Manual
    edits are allowed to violate norms (a designer may have a legitimate
    reason) but must surface a clear warning when they do -- the frontend
    runs this once when a plan opens, and validate_items (below) after each
    edit. Zones come from exclusion_cache, so repeated calls don't rebuild
    them.
    """
    return _check_violations(project, plan.items)


def validate_items(project: Project, plan: Plan, ids: list[str]) -> list[dict]:
    """Same check as validate_plan, restricted to specific items -- the
    setback check is per-item and independent of every other planting (it
    only compares against the fixed exclusion zone, never against other
    items), so an edit can only ever change violation status for the items
    it actually touched. The frontend calls this instead of a full
    validate_plan after every move/retype/restore, merging the result into
    its own violation set by id -- avoids a full plan's worth of checks
    (cheap per item thanks to exclusion_cache, but still linear in plan size)
    on every single edit.
    """
    wanted = set(ids)
    return _check_violations(project, [item for item in plan.items if item.id in wanted])
