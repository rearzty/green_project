"""Охранные зоны вокруг точки — то, что проступает на карте, пока тянешь
посадку (п. 2 редизайна, Drag.dc.html).

Отдаётся геометрия самих ограничений (линия сети, полигон здания, соседнее
дерево), их охранная зона и требуемый отступ. **Расстояние до них считает
клиент** на каждом кадре перетаскивания (frontend/lib/dragNorms.ts): запрос на
кадр был бы медленнее самого жеста, а геометрия вокруг точки не меняется,
пока её тянут.

Нормы те же, что у проверки плана: `norms.resolve_setback` по типу и породе
(поправки на крону и теплотрассу), буфер у зданий и зон — с острым углом
(`mitre`), у сетей — круглый, как в `geo_engine.buffers.build_exclusion_zone`.
Так показанная зона совпадает с той, по которой посадка потом считается
нарушением, а не рисует «примерно такую же».
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np
import shapely
from shapely import STRtree
from shapely.geometry import Point, box, mapping
from shapely.geometry.base import BaseGeometry

from backend.app.db.models import Plan, Project
from backend.app.services.edit_service import _catalogue, _crown_interval
from backend.app.services.geo_io import _to_wgs84, from_wgs84, layers_to_domain
from backend.app.services.pipeline_service import plan_norms
from geo_engine.compliance import NEIGHBOUR_OBJECT_TYPE, OBJECT_LABELS_RU, SPACING_CHECKED_TYPES

DEFAULT_RADIUS_M = 15.0
MAX_RADIUS_M = 60.0
# Больше этого вокруг точки не отдаём: на узле сетей их сотни, а глазу и
# подписи хватает ближайших.
MAX_CONSTRAINTS = 250
_BUFFER_QUAD_SEGS = 16
_MAX_PROJECTS_CACHED = 16


@dataclass
class _Index:
    geometries: np.ndarray
    types: list[str]
    tree: STRtree


_cache: "OrderedDict[tuple, _Index]" = OrderedDict()
_lock = threading.Lock()


def _constraint_index(project: Project, setback_types: frozenset[str]) -> _Index | None:
    """Геометрии всех ограничений проекта и индекс по ним. Слои после загрузки
    не меняются, поэтому индекс строится один раз на проект (и набор типов зон
    из норм), а не на каждое перетаскивание: построение по десяткам тысяч
    отрезков — доли секунды, а цель — зоны раньше 200 мс."""
    key = (str(project.id), tuple(sorted(setback_types)))
    with _lock:
        found = _cache.get(key)
        if found is not None:
            _cache.move_to_end(key)
            return found
    utilities, zones = layers_to_domain(project.layers)
    geometries: list[BaseGeometry] = []
    types: list[str] = []
    for utility in utilities:
        if utility.geometry is not None and not utility.geometry.is_empty:
            geometries.append(utility.geometry)
            types.append(utility.object_type)
    for zone in zones:
        if zone.zone_type in setback_types and zone.geometry is not None and not zone.geometry.is_empty:
            geometries.append(zone.geometry)
            types.append(zone.zone_type)
    if not geometries:
        return None
    array = np.empty(len(geometries), dtype=object)
    array[:] = geometries
    index = _Index(geometries=array, types=types, tree=STRtree(array))
    with _lock:
        _cache[key] = index
        while len(_cache) > _MAX_PROJECTS_CACHED:
            _cache.popitem(last=False)
    return index


def _shown(geometry: BaseGeometry, source_crs: str | None) -> dict | None:
    if geometry.is_empty:
        return None
    return mapping(_to_wgs84(geometry, source_crs))


def nearby_constraints(
    plan: Plan,
    x: float,
    y: float,
    radius_m: float,
    planting_type: str,
    item_id: str | None,
    source_crs: str | None,
) -> dict:
    """Ограничения в `radius_m` от точки (x, y) — в координатах отображения.

    `item_id` — посадка, которую тянут: от её породы зависят и отступы, и
    интервал до соседних деревьев; без неё считается порода «по умолчанию».
    """
    project = plan.project
    norms = plan_norms(plan)
    catalogue = _catalogue()
    row = next((r for r in plan.items if r.id == item_id), None) if item_id else None
    species = catalogue.get(row.species) if row is not None else None
    centre = from_wgs84(Point(x, y), source_crs)
    radius = min(max(radius_m, 1.0), MAX_RADIUS_M)

    features: list[dict] = []
    index = _constraint_index(project, frozenset(norms.setbacks_m))
    if index is not None:
        hits = index.tree.query(centre, predicate="dwithin", distance=radius)
        if len(hits) > MAX_CONSTRAINTS:
            distance = shapely.distance(index.geometries[hits], centre)
            hits = hits[np.argsort(distance)[:MAX_CONSTRAINTS]]
        for hit in hits:
            object_type = index.types[int(hit)]
            geometry = index.geometries[int(hit)]
            required = norms.resolve_setback(object_type, planting_type, species, catalogue.crown_reference_diameter_m).required_m
            is_area = geometry.geom_type in ("Polygon", "MultiPolygon")
            zone = geometry.buffer(required, quad_segs=_BUFFER_QUAD_SEGS, join_style="mitre" if is_area else "round")
            # Объекты тянутся на сотни метров, а на карте нужен только участок
            # вокруг точки — режем по квадрату, иначе ответ весит как вся сеть.
            window = box(centre.x - radius - required, centre.y - radius - required, centre.x + radius + required, centre.y + radius + required)
            clipped_zone = shapely.clip_by_rect(zone, *window.bounds)
            clipped_object = shapely.clip_by_rect(geometry, *window.bounds)
            shown_zone = _shown(clipped_zone, source_crs)
            if shown_zone is None:
                continue
            features.append(
                {
                    "kind": "constraint",
                    "object_type": object_type,
                    "object_label": OBJECT_LABELS_RU.get(object_type, object_type),
                    "required_m": round(float(required), 2),
                    "geometry": _shown(clipped_object, source_crs),
                    "zone": shown_zone,
                }
            )

    # Соседние деревья: интервал по классу кроны — больший из двух пород.
    if planting_type in SPACING_CHECKED_TYPES:
        own = _crown_interval(catalogue, row.species) if row is not None else None
        for other in plan.items:
            if other.planting_type not in SPACING_CHECKED_TYPES or other.id == item_id:
                continue
            geometry = other.geometry
            # Прямой доступ к x/y без db_to_shape: на плане в сотни тысяч посадок
            # это единственный цикл по всем, и он должен быть дешёвым.
            if abs(geometry.x - centre.x) > radius or abs(geometry.y - centre.y) > radius:
                continue
            known = [v for v in (own, _crown_interval(catalogue, other.species)) if v]
            if not known:
                continue
            required = max(known)
            features.append(
                {
                    "kind": "neighbour",
                    "id": other.id,
                    "object_type": NEIGHBOUR_OBJECT_TYPE,
                    "object_label": OBJECT_LABELS_RU.get(NEIGHBOUR_OBJECT_TYPE, "Соседнее дерево"),
                    "required_m": round(float(required), 2),
                    "geometry": _shown(geometry, source_crs),
                    "zone": _shown(geometry.buffer(required, quad_segs=_BUFFER_QUAD_SEGS), source_crs),
                }
            )
    return {"radius_m": radius, "features": features}
