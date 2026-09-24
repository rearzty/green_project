"""Finding the site outline among the zones a reader produced.

Lives in geo_engine rather than in the backend because it is pure geometry and
because the CLI needs it without dragging in SQLAlchemy. `pipeline_service` and
`edit_service` re-export it under the names they already used.
"""

from __future__ import annotations

import sys

from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.strtree import STRtree

from geo_engine.model import Utility, Zone

TERRITORY_ZONE_TYPE = "territory"
_AREAL = ("Polygon", "MultiPolygon")

# Во сколько раз дальше собственного размера может отстоять кусок участка,
# прежде чем считать его чужим. Участок пространственно связен: на эталонной
# улице его две части соприкасаются и при объединении дают один полигон.
# Кусок, лежащий в нескольких своих диагоналях от основного, — это либо другой
# объект, либо файл в другой системе координат, а не часть той же площадки.
MAX_PART_DISTANCE_RATIO = 2.0

# Кусок, не прошедший дистанционный тест выше, не отбрасывается сразу, если
# рядом с ним (в этом радиусе) реально есть чужая инфраструктура — сети или
# другие зоны. Число и радиус — не подобраны абстрактно, а разделяют два
# живых случая пилота ровно по этому признаку (см. _has_nearby_context):
# у мусорного куска (4. Харьковская улица, кусок в 3991м от опоры) в радиусе
# 100м реально 0 сетей и 0 других зон; у настоящего второго участка съёмки
# (13. Харьковский проезд, 11 кусков в 4.2-4.8км от опоры — тот же порядок
# расстояния, что и у мусора, так что дистанция здесь НЕ разделяющий признак)
# — минимум 12 сетей и тысячи других зон в том же радиусе. Порог 5 — с
# большим запасом выше «0» и с большим запасом ниже «12+», не подгонка под
# конкретные числа.
_CONTEXT_BUFFER_M = 100.0
_MIN_NEARBY_CONTEXT_TO_KEEP = 5


class MissingTerritoryError(ValueError):
    """The input has no territory boundary zone -- nothing can be generated.

    Message is shown to the user as-is."""


def territory_polygon(zones: list[Zone], utilities: list[Utility] | None = None) -> BaseGeometry:
    """The site outline: every areal territory zone, unioned.

    Unions rather than returning the first match, and ignores zones that are not
    areal at all. Both matter on real drawings: the pilot street's work area is
    genuinely two polygons (the street splits around a block, 29739 + 16132 m²),
    so taking one would silently drop a third of the site; and the same layer in
    the main drawing carries leftover 2- and 5-vertex fragments that arrive as
    LineStrings, which would otherwise be returned as "the territory" and make
    every candidate fall outside it.

    `utilities` is optional (existing call sites/tests that only have zones keep
    working unchanged) but should be passed wherever available — it feeds
    `_dominant_cluster`'s nearby-context check, see there for why that matters.
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

    context = [
        zone.geometry
        for zone in zones
        if zone.zone_type != TERRITORY_ZONE_TYPE and zone.geometry is not None and not zone.geometry.is_empty
    ]
    context.extend(
        utility.geometry for utility in (utilities or []) if utility.geometry is not None and not utility.geometry.is_empty
    )

    kept, dropped = _dominant_cluster(areas, context)
    if dropped:
        # Печатается, а не проглатывается: молча выброшенный кусок участка
        # выглядел бы как «территория чуть меньше, чем ожидалось», и искать
        # причину пришлось бы в геометрии, а не в данных.
        print(
            f"  ! отброшено контуров участка, удалённых от основного: {len(dropped)} "
            f"(суммарно {sum(g.area for g in dropped):,.0f} м²)".replace(",", " "),
            file=sys.stderr,
        )
    return kept[0] if len(kept) == 1 else _combine_with_holes(kept)


def _combine_with_holes(pieces: list[BaseGeometry]) -> BaseGeometry:
    """Union, except a piece (almost) entirely inside a bigger one already
    combined is cut out as a hole, not added as redundant area.

    Live case, «1. Олимпийская деревня»: the same layer («Границы работ»)
    carries one wide outer boundary (514 206 м², drawn as two open polylines
    that close against each other -- see RECONSTRUCT_FOOTPRINT_ZONE_TYPES's
    comment) plus 8 much smaller, separately-drawn rings, each 100% inside
    that outer one (checked directly: every one of the 8 has
    `intersection(outer).area / own_area == 1.0`). A plain `unary_union` of
    all 9 is a no-op here (514 206 м² either way) because none of the small
    ones stick out past the big one -- the union operation has no way to
    know the small rings mean "exclude this scope", not "here is more area
    to cover". The site's real, intended work area is the outer boundary
    MINUS those 8 (421 824 м²), matching the drawing's own second dashed
    boundary the small rings trace and the user's own confirmation that
    these are cut-outs, not additional territory.

    Processing biggest-first and testing containment against the geometry
    *built so far* (not the original pieces) also does the geometrically
    right thing for a hole-within-a-hole (an island sitting inside an
    already-excluded hole): once that hole is subtracted, the island no
    longer overlaps the shrunk `result` at all, so it falls through to the
    plain union branch and gets added back -- exactly the classic
    polygon-with-holes-and-islands semantics, not something special-cased
    for this one street.

    Genuinely disjoint or partially-overlapping pieces (the reference
    street's two co-equal 29 739 + 16 132 m² halves, or 13. Харьковский
    проезд's two survey clusters) are untouched: neither sits (almost)
    fully inside the other, so both go through the plain union branch,
    exactly as before this function existed.
    """
    by_area_desc = sorted(pieces, key=lambda g: -g.area)
    result = by_area_desc[0]
    for piece in by_area_desc[1:]:
        if piece.area <= 0:
            continue
        contained_fraction = piece.intersection(result).area / piece.area
        if contained_fraction > 0.99:
            result = result.difference(piece)
        else:
            result = unary_union([result, piece])
    return result


def _has_nearby_context(geometry: BaseGeometry, context: list[BaseGeometry], tree: STRtree | None) -> bool:
    """Есть ли рядом с куском достаточно чужой инфраструктуры, чтобы считать
    его настоящим, а не мусором — см. константы выше за числами и живыми
    примерами, на которых порог подобран."""
    if tree is None:
        return False
    buffered = geometry.buffer(_CONTEXT_BUFFER_M)
    count = 0
    for idx in tree.query(buffered):
        if context[int(idx)].intersects(buffered):
            count += 1
            if count >= _MIN_NEARBY_CONTEXT_TO_KEEP:
                return True
    return False


def _dominant_cluster(
    areas: list[BaseGeometry], context: list[BaseGeometry] | None = None
) -> tuple[list[BaseGeometry], list[BaseGeometry]]:
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

    Кусок, не прошедший дистанционный порог, — не автоматически мусор.
    13. Харьковский проезд — один заказ на съёмку («output[1-6]_...», шесть
    блоков), и его реальная площадка честно распадается на два кластера в
    4.2-4.8 км друг от друга: расстояние тут ТАКОГО ЖЕ порядка, что и у
    настоящего мусора на 4. Харьковской (3991 м) — дистанция одна не
    разделяет эти два случая. Разделяет то, что рядом: у харьковского мусора
    в буфере 100м от кластера — 0 сетей и 0 других зон, у обоих кластеров
    харьковского проезда — от 12 до 178 сетей и тысячи других зон. Поэтому
    кусок, далёкий по дистанции, всё равно остаётся, если рядом с ним есть
    настоящая инфраструктура, и отбрасывается только если он по-настоящему
    изолирован.
    """
    anchor = max(areas, key=lambda g: g.area)
    minx, miny, maxx, maxy = anchor.bounds
    diagonal = ((maxx - minx) ** 2 + (maxy - miny) ** 2) ** 0.5
    limit = diagonal * MAX_PART_DISTANCE_RATIO

    tree = STRtree(context) if context else None

    kept, dropped = [], []
    for geometry in areas:
        if geometry.distance(anchor) <= limit or _has_nearby_context(geometry, context or [], tree):
            kept.append(geometry)
        else:
            dropped.append(geometry)
    return kept, dropped
