"""Слои для 3D-просмотра — только то, что сцена рисует, и только вокруг участка.

Замечание экспертов: «на некоторых данных 3D не отображается или
отображается с ошибкой». Причины нашлись в том, что 3D получал на вход:

* весь проект целиком (`GET .../layers`) — на крупной улице сотни тысяч
  объектов и сотни мегабайт JSON, которые браузер может просто не дожевать;
* здания, дороги и зелень всей СЪЁМКИ, а не участка — съёмка бывает на
  километры (у «9. Измайловская площадь» куски в 3 км друг от друга), камера
  отъезжала так далеко, что посадки превращались в точки, а глубина теряла
  точность;
* точку отсчёта — первый попавшийся контур слоя границы, хотя
  `territory_polygon` мог его отбросить как обрывок;
* невалидные полигоны из чертежей (самопересечения) — их триангуляция в
  three.js даёт рваные грани.

Здесь всё это решается на сервере: участок — тот же итоговый, по которому
строится план (`exclusion_cache.with_territory`), здания берутся целиком, если
касаются окрестности участка, дороги и зелень обрезаются по ней, невалидное
чинится, неплощадное отбрасывается (сцена его всё равно не рисует).
"""

from __future__ import annotations

import shapely
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from backend.app.db.models import Project
from backend.app.schemas.geo import GeoJSONFeature, GeoJSONFeatureCollection, GeoJSONGeometry
from backend.app.services import exclusion_cache
from backend.app.services.geo_io import _to_wgs84, db_to_shape, display_crs
from geo_engine.territory import MissingTerritoryError

# Сколько окружения показывать вокруг участка: здания и дороги по соседству
# нужны, чтобы план читался в контексте улицы, а не висел в пустоте.
CONTEXT_MARGIN_M = 30.0

# Что 3D-сцена рисует (frontend/lib/plan3d.ts::buildScene3DData) — остальное
# туда незачем и везти.
_SCENE_ZONE_TYPES = ("building", "road", "existing_greenery")


def _polygonal(geometry: BaseGeometry) -> BaseGeometry | None:
    if geometry is None or geometry.is_empty:
        return None
    if not geometry.is_valid:
        geometry = shapely.make_valid(geometry)
    if geometry.geom_type in ("Polygon", "MultiPolygon"):
        return geometry
    parts = [g for g in getattr(geometry, "geoms", []) if g.geom_type in ("Polygon", "MultiPolygon") and not g.is_empty]
    if not parts:
        return None
    return shapely.union_all(parts)


def scene_layers(project: Project) -> GeoJSONFeatureCollection:
    try:
        territory = exclusion_cache.with_territory(project, lambda t: t)
    except MissingTerritoryError:
        territory = None

    crs = display_crs(project)
    features: list[GeoJSONFeature] = []

    def add(geometry: BaseGeometry, kind: str, object_type: str) -> None:
        features.append(
            GeoJSONFeature(
                geometry=GeoJSONGeometry(**mapping(_to_wgs84(geometry, crs))),
                properties={"kind": kind, "object_type": object_type},
            )
        )

    region = territory.buffer(CONTEXT_MARGIN_M) if territory is not None else None
    if territory is not None:
        add(territory, "zone", "territory")
    if region is not None:
        shapely.prepare(region)

    for layer in project.layers:
        if layer.kind != "zone" or layer.object_type not in _SCENE_ZONE_TYPES:
            continue
        geometry = _polygonal(db_to_shape(layer.geometry))
        if geometry is None:
            continue
        if region is not None:
            if not region.intersects(geometry):
                continue
            if layer.object_type != "building":
                # Здание режется пополам некрасиво и неправдоподобно — оно
                # целиком; полотно дороги и массив зелени тянутся на
                # километры, их обрезаем.
                geometry = _polygonal(geometry.intersection(region))
                if geometry is None:
                    continue
        add(geometry, "zone", layer.object_type)

    return GeoJSONFeatureCollection(features=features)
