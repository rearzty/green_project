"""Manual plan corrections: single-item patches, id-based batch edits (what
the map's selection tool and the frontend's local undo/redo drive), and
normative validation.

User-facing error messages here are Russian on purpose -- the frontend shows
them to the designer as-is in its toast notifications.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np
import shapely
from shapely import STRtree
from shapely.affinity import translate
from shapely.geometry import LineString, Point, shape
from shapely.geometry.base import BaseGeometry

from backend.app.db.models import Plan, PlantingItemRow, Project
from backend.app.services import exclusion_cache
from backend.app.services.geo_io import db_to_shape, from_wgs84, planting_items_to_geojson_dicts, shape_to_db
from geo_engine.compliance import SPACING_CHECKED_TYPES
from geo_engine.species import SpeciesCatalogue, load_catalogue

_POINT_PLANTING_TYPES = {"tree", "shrub"}
_POLYGON_PLANTING_TYPES = {"lawn"}
_PLANTING_TYPE_LABELS = {"tree": "Дерево", "shrub": "Кустарник", "lawn": "Газон"}


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


class RespaceError(ValueError):
    """Ряд нельзя перерасставить с таким шагом: не ряд (одна посадка, площадной
    объект, смесь типов), шаг меньше нормы по классу кроны или в ряд не
    помещается ни одной посадки. Сообщение — готовый русский текст."""


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


def apply_item_patch(
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
    return item


def delete_item(item: PlantingItemRow) -> None:
    """Remove a single planting item by id."""
    plan = item.plan
    plan.items = [i for i in plan.items if i.id != item.id]
    plan.item_count -= 1
    plan.has_manual_edits = True  # see Plan's docstring: never pruned by _prune_stale_plans once a human has touched it


def delete_items(plan: Plan, ids: list[str], source_crs: str | None) -> list[dict]:
    """Batch delete by id (the selection tool's Del, the redo of a delete).
    Returns each item's pre-delete GeoJSON snapshot -- exactly what
    restore_items needs to undo it.
    """
    items = _items_by_ids(plan, ids)
    snapshots = planting_items_to_geojson_dicts(items, source_crs)
    remove_ids = {item.id for item in items}
    plan.items = [item for item in plan.items if item.id not in remove_ids]
    plan.item_count -= len(items)
    plan.has_manual_edits = True
    return snapshots


# Больше этого в одном ряду не бывает (самый длинный ряд реальной улицы — сотни
# посадок): жадный обход соседей квадратичен, и «ряд» из десятков тысяч точек —
# это выделенный весь план, а не ряд.
_MAX_RESPACE_ITEMS = 5_000
# Кольцо ориентира (граница участка) замкнуто: зазор между концами цепочки не
# больше полутора обычных шагов — иначе это обычный ряд с концами.
_RING_GAP_FACTOR = 1.5


def chain_indices(points: list[Point]) -> list[int]:
    """Номера посадок ряда в порядке обхода: от крайней к соседней, жадно. Ряд —
    это линия с почти равными промежутками, поэтому ближайший непосещённый
    сосед и есть следующая точка ряда. Индексы, а не точки: «исправить
    нарушение» (fix_service) должен знать, какая именно посадка стоит рядом."""
    xs = np.array([p.x for p in points])
    ys = np.array([p.y for p in points])
    start = int(np.argmax((xs - xs.mean()) ** 2 + (ys - ys.mean()) ** 2))  # дальняя от центра — конец ряда
    order = [start]
    remaining = set(range(len(points))) - {start}
    while remaining:
        last = order[-1]
        nxt = min(remaining, key=lambda i: (xs[i] - xs[last]) ** 2 + (ys[i] - ys[last]) ** 2)
        order.append(nxt)
        remaining.remove(nxt)
    return order


def _chain_order(points: list[Point]) -> list[Point]:
    """Те же посадки ряда, но самими точками."""
    return [points[i] for i in chain_indices(points)]


def respace_items(plan: Plan, ids: list[str], pitch_m: float, source_crs: str | None) -> tuple[list[dict], list[dict]]:
    """Ряд перерасставляется с другим шагом — «Изменить интервал» на панели
    выделения. Точки одного ряда выстраиваются в линию обходом соседей и
    заново раскладываются по ней с шагом `pitch_m`; первая и последняя остаются
    краями ряда (ряд остаётся на той же улице), остаток длины делится поровну по
    концам. У замкнутого кольца шаг подгоняется так, чтобы он делил длину
    целиком и был не меньше заданного — тот же приём, что у ряда на этапе
    генерации (patterns.points_along), иначе на стыке образовалась бы щель или
    нарушение интервала.

    Для деревьев шаг не может быть меньше нормы по классу кроны — её проверяет
    и отчёт (МГСН 1.02-02 п. 4.2.9.2), и отказ здесь честнее, чем подсвеченное
    красным нарушение сразу после «Применить». Кусту интервал в ряду норма не
    задаёт (743-ПП табл. 3.6.2 — расстояние внутри посадки, не минимум).

    Прежние посадки заменяются новыми (новые id, порода/оценка/обоснование —
    от ближайшей прежней, `group_id` сохраняется, `is_manual_edit=True`).
    Возвращает (снимки прежних, снимки новых) — отмена удаляет новые и
    возвращает прежние теми же эндпоинтами, что и остальные правки. Точки
    нового ряда вне участка отбрасываются, как и всякая правка не выходит за его
    границу; не осталось ни одной — отказ.
    """
    rows = _items_by_ids(plan, ids)
    if len(rows) < 2:
        raise RespaceError("В ряду должно быть не меньше двух посадок — выделите ряд целиком.")
    if len(rows) > _MAX_RESPACE_ITEMS:
        raise RespaceError("Слишком много посадок для одного ряда — выделите ряд, а не весь план.")
    types = {row.planting_type for row in rows}
    if types - _POINT_PLANTING_TYPES or len(types) != 1:
        raise RespaceError("Интервал меняется у ряда деревьев или кустарника, а не у газона и не у смеси типов.")
    (planting_type,) = types

    if planting_type in SPACING_CHECKED_TYPES:
        catalogue = load_catalogue()
        required = max((_crown_interval(catalogue, row.species) or 0.0 for row in rows), default=0.0)
        if pitch_m < required - 1e-6:
            shown = f"{required:g}".replace(".", ",")
            raise RespaceError(f"Деревья этого ряда нельзя ставить чаще, чем через {shown} м: таков интервал по классу кроны (МГСН 1.02-02 п. 4.2.9.2).")

    old_points = [db_to_shape(row.geometry) for row in rows]
    ordered = _chain_order(old_points)
    steps = [a.distance(b) for a, b in zip(ordered, ordered[1:])]
    typical = float(np.median(steps)) if steps else pitch_m
    ring = len(ordered) >= 3 and ordered[0].distance(ordered[-1]) <= typical * _RING_GAP_FACTOR
    path = [(p.x, p.y) for p in ordered] + ([(ordered[0].x, ordered[0].y)] if ring else [])
    line = LineString(path)
    if ring:
        count = int(line.length // pitch_m)
        step = line.length / count if count else pitch_m
        distances = [i * step for i in range(count)]
    else:
        count = int(line.length // pitch_m) + 1
        lead = (line.length - (count - 1) * pitch_m) / 2
        distances = [lead + i * pitch_m for i in range(count)]
    if len(distances) < 2:
        raise RespaceError("Ряд короче этого шага — в него не помещается и двух посадок.")
    new_points = [line.interpolate(d) for d in distances]

    def _inside(territory: BaseGeometry) -> np.ndarray:
        return np.asarray(shapely.within(np.array(new_points, dtype=object), territory.buffer(_TERRITORY_TOLERANCE_M)))

    inside = exclusion_cache.with_territory(plan.project, _inside)
    kept = [p for p, ok in zip(new_points, inside) if ok]
    if not kept:
        raise OutOfTerritoryError("Нельзя разместить посадку за границей участка: с таким шагом ни одна точка ряда не остаётся внутри.")

    tree = STRtree(old_points)
    nearest = tree.nearest(kept)
    created = [
        PlantingItemRow(
            plan_id=plan.id,
            geometry=shape_to_db(point),
            planting_type=planting_type,
            species=rows[int(index)].species,
            score=rows[int(index)].score,
            rationale=rows[int(index)].rationale,
            group_id=rows[int(index)].group_id,
            is_manual_edit=True,
            plan=plan,
        )
        for point, index in zip(kept, nearest)
    ]

    previous = planting_items_to_geojson_dicts(rows, source_crs)
    removed = {row.id for row in rows}
    plan.items = [item for item in plan.items if item.id not in removed] + created
    plan.item_count += len(created) - len(rows)
    plan.has_manual_edits = True
    return previous, planting_items_to_geojson_dicts(created, source_crs)


def retype_items(plan: Plan, changes: list[tuple[str, str]], source_crs: str | None) -> tuple[list[dict], list[str]]:
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
    return previous, skipped


def move_items(
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
    return planting_items_to_geojson_dicts(items, source_crs)


def restore_items(plan: Plan, features: list[dict], source_crs: str | None) -> None:
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

    restored = [
        PlantingItemRow(
            id=feature["properties"]["id"],
            plan_id=plan.id,
            geometry=shape_to_db(geometry),
            planting_type=feature["properties"]["planting_type"],
            species=feature["properties"].get("species", "default"),
            score=feature["properties"].get("score", 0.0),
            rationale=feature["properties"].get("rationale", ""),
            # Undo puts the item back exactly as it was -- a generated item stays "generated".
            is_manual_edit=bool(feature["properties"].get("is_manual_edit", False)),
            # Вернувшаяся посадка остаётся в своём ряду.
            group_id=feature["properties"].get("group_id"),
            plan=plan,
        )
        for feature, geometry in zip(features, geometries)
    ]
    plan.items = plan.items + restored
    plan.item_count += len(restored)
    plan.has_manual_edits = True


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


def _check_violations(project: Project, items: list[PlantingItemRow], trees_in_paving: bool = False) -> list[dict]:
    # По (тип, порода), а не только по типу: у деревьев отступ зависит от
    # породы (см. exclusion_cache._species_key) — и в одном плане их бывает
    # несколько (ряд одной породой, россыпь другой).
    by_type: dict[tuple[str, str | None], list[PlantingItemRow]] = {}
    for item in items:
        by_type.setdefault((item.planting_type, item.species), []).append(item)

    violations: list[dict] = []
    for (planting_type, species), type_items in by_type.items():
        geometries = [db_to_shape(item.geometry) for item in type_items]
        mask = exclusion_cache.with_exclusion_zone(
            project, planting_type, lambda zone: violation_mask(geometries, zone), species=species,
            trees_in_paving=trees_in_paving,
        )
        label = _PLANTING_TYPE_LABELS.get(planting_type, planting_type)
        violations.extend(
            {"item_id": item.id, "message": f"{label}: нарушен норматив отступа от сетей или зданий."}
            for item, violated in zip(type_items, mask)
            if violated
        )
    return violations


# Тот же допуск, что у geo_engine.compliance: веб и отчёт обоснований не
# должны расходиться в вердикте на одной и той же паре деревьев.
_SPACING_TOLERANCE_M = 1e-3


@lru_cache(maxsize=1)
def _catalogue() -> SpeciesCatalogue:
    return load_catalogue()


def _crown_interval(catalogue: SpeciesCatalogue, species: str | None) -> float | None:
    found = catalogue.get(species)
    return catalogue.spacing_for_crown(found.crown) if found is not None else None


def _spacing_violations(plan: Plan, ids: list[str] | None) -> tuple[list[dict], set[str]]:
    """Интервал между стволами соседних деревьев (МГСН 1.02-02 п. 4.2.9.2).

    Та же семантика, что `geo_engine.compliance._neighbour_checks` — до
    ближайшего соседа, требуется больший из интервалов двух пород, — чтобы
    подсветка на карте и отчёт обоснований никогда не расходились. Раньше веб
    этого не проверял вовсе: два дерева, сдвинутые руками вплотную, на карте
    выглядели законными.

    Вторым значением — чьи вердикты пересчитаны. При точечной проверке это не
    только сами `ids`: у дерева рядом с перенесённым мог смениться ближайший
    сосед, и его подсветку тоже надо обновить — поэтому в пересчёт идут все
    деревья в пределах наибольшего интервала каталога от тронутых.
    """
    trees = [item for item in plan.items if item.planting_type in SPACING_CHECKED_TYPES]
    if len(trees) < 2:
        return [], set(ids or ())
    catalogue = _catalogue()
    geometries = np.empty(len(trees), dtype=object)
    geometries[:] = [db_to_shape(item.geometry) for item in trees]
    index = STRtree(geometries)

    if ids is None:
        check = np.arange(len(trees))
    else:
        wanted = set(ids)
        seed = [k for k, item in enumerate(trees) if item.id in wanted]
        if not seed:
            return [], wanted
        reach = max((v for v in catalogue.crown_spacing_m.values() if v), default=0.0)
        near = index.query(geometries[seed], predicate="dwithin", distance=reach)
        check = np.unique(np.concatenate([np.array(seed), near[1]]))

    pairs, distances = index.query_nearest(geometries[check], exclusive=True, all_matches=False, return_distance=True)
    violations: list[dict] = []
    for local, neighbour, distance in zip(pairs[0], pairs[1], distances):
        own = trees[int(check[int(local)])]
        other = trees[int(neighbour)]
        known = [v for v in (_crown_interval(catalogue, own.species), _crown_interval(catalogue, other.species)) if v]
        if not known:
            continue
        required = max(known)
        if float(distance) + _SPACING_TOLERANCE_M < required:
            violations.append(
                {
                    "item_id": own.id,
                    "message": (
                        f"Дерево: до соседнего дерева {float(distance):.2f} м при интервале {required:.1f} м "
                        "(МГСН 1.02-02 п. 4.2.9.2)."
                    ),
                }
            )
    checked = {trees[int(k)].id for k in check}
    return violations, checked | set(ids or ())


def validate_plan(project: Project, plan: Plan) -> list[dict]:
    """Re-check every item in the plan against the setback rulebook. Manual
    edits are allowed to violate norms (a designer may have a legitimate
    reason) but must surface a clear warning when they do -- the frontend
    runs this once when a plan opens, and validate_items (below) after each
    edit. Zones come from exclusion_cache, so repeated calls don't rebuild
    them.
    """
    spacing, _ = _spacing_violations(plan, None)
    return _check_violations(project, plan.items, plan.trees_in_paving) + spacing


def validate_items_scoped(project: Project, plan: Plan, ids: list[str]) -> tuple[list[dict], list[str]]:
    """`validate_items` плюс список id, чьи вердикты пересчитаны (тронутые и
    их соседи по интервалу) — фронт заменяет подсветку именно у них, см.
    `_spacing_violations`."""
    wanted = set(ids)
    setback = _check_violations(project, [item for item in plan.items if item.id in wanted], plan.trees_in_paving)
    spacing, checked = _spacing_violations(plan, ids)
    return setback + spacing, sorted(checked)


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

    Интервал между деревьями — исключение из «только тронутые»: он зависит
    от соседей, поэтому может вернуть нарушение и у соседа тронутого дерева
    (см. `validate_items_scoped`).
    """
    return validate_items_scoped(project, plan, ids)[0]
