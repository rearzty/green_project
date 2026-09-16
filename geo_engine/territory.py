"""Finding the site outline among the zones a reader produced.

Lives in geo_engine rather than in the backend because it is pure geometry and
because the CLI needs it without dragging in SQLAlchemy. `pipeline_service` and
`edit_service` re-export it under the names they already used.
"""

from __future__ import annotations

import sys

from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from geo_engine.model import Zone

TERRITORY_ZONE_TYPE = "territory"
_AREAL = ("Polygon", "MultiPolygon")

# Во сколько раз дальше собственного размера может отстоять кусок участка,
# прежде чем считать его чужим. Участок пространственно связен: на эталонной
# улице его две части соприкасаются и при объединении дают один полигон.
# Кусок, лежащий в нескольких своих диагоналях от основного, — это либо другой
# объект, либо файл в другой системе координат, а не часть той же площадки.
MAX_PART_DISTANCE_RATIO = 2.0


class MissingTerritoryError(ValueError):
    """The input has no territory boundary zone -- nothing can be generated.

    Message is shown to the user as-is."""


def territory_polygon(zones: list[Zone]) -> BaseGeometry:
    """The site outline: every areal territory zone, unioned.

    Unions rather than returning the first match, and ignores zones that are not
    areal at all. Both matter on real drawings: the pilot street's work area is
    genuinely two polygons (the street splits around a block, 29739 + 16132 m²),
    so taking one would silently drop a third of the site; and the same layer in
    the main drawing carries leftover 2- and 5-vertex fragments that arrive as
    LineStrings, which would otherwise be returned as "the territory" and make
    every candidate fall outside it.
    """
    areas = [
        zone.geometry
        for zone in zones
        if zone.zone_type == TERRITORY_ZONE_TYPE
        and zone.geometry is not None
        and not zone.geometry.is_empty
        and zone.geometry.geom_type in _AREAL
    ]
    if not areas:
        raise MissingTerritoryError(
            "В загруженном файле нет границы участка (слой territory / BOUNDARY) — план построить нельзя."
        )
    if len(areas) == 1:
        return areas[0]

    kept, dropped = _dominant_cluster(areas)
    if dropped:
        # Печатается, а не проглатывается: молча выброшенный кусок участка
        # выглядел бы как «территория чуть меньше, чем ожидалось», и искать
        # причину пришлось бы в геометрии, а не в данных.
        print(
            f"  ! отброшено контуров участка, удалённых от основного: {len(dropped)} "
            f"(суммарно {sum(g.area for g in dropped):,.0f} м²)".replace(",", " "),
            file=sys.stderr,
        )
    return kept[0] if len(kept) == 1 else unary_union(kept)


def _dominant_cluster(areas: list[BaseGeometry]) -> tuple[list[BaseGeometry], list[BaseGeometry]]:
    """Разделить контуры на «этот участок» и «что-то другое».

    Нужно потому, что бандл склеивается конкатенацией, а в пилотных данных
    нашёлся файл внешней ссылки в ДРУГОЙ системе координат: его контур
    объединялся с настоящим, `territory_polygon` возвращала мультиполигон из
    двух кусков в трёх километрах друг от друга, и половина посадок уезжала
    туда, где нет ни одной сети. План при этом считался полностью
    соответствующим нормативам — просто потому, что ближайшее ограничение было
    за три километра. Тихий неверный результат, который на глаз не отличить от
    верного.

    Опорой берётся самый крупный контур: если в данных смешаны два объекта,
    больший почти наверняка и есть заказанный участок.
    """
    anchor = max(areas, key=lambda g: g.area)
    minx, miny, maxx, maxy = anchor.bounds
    diagonal = ((maxx - minx) ** 2 + (maxy - miny) ** 2) ** 0.5
    limit = diagonal * MAX_PART_DISTANCE_RATIO

    kept, dropped = [], []
    for geometry in areas:
        (kept if geometry.distance(anchor) <= limit else dropped).append(geometry)
    return kept, dropped
