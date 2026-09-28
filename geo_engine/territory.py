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
    result = kept[0] if len(kept) == 1 else _combine_with_holes(kept)
    if result.is_empty or result.area <= 0:
        # Вторая, независимая защита от того же класса отказа. Слой границы в
        # чертеже ЕСТЬ, но после кластеризации и вычитания дырок от него не
        # осталось площади — а дальше по пайплайну пустая территория даёт
        # пустой план, и CLI рапортует «Готово» с кодом возврата 0. Живьём на
        # «4. Харьковская улица» так и было: отчёт с нулём посадок, нулём
        # нарушений и успешным завершением, то есть пользователь получал молча
        # неверный результат вместо ошибки. Конкретную причину там починила
        # дедупликация контуров (см. `_without_duplicates`), но сам по себе
        # успех на пустой территории — отдельный дефект, и закрывать его надо
        # отдельно: следующая такая геометрия придёт с другой улицы.
        raise MissingTerritoryError(
            "Граница участка в чертеже есть, но после объединения контуров от неё "
            "не осталось площади — план построить нельзя. Так бывает, когда контуры "
            "накладываются друг на друга и взаимно вычитаются как внутренние кольца."
        )
    return result


def _as_polygonal(geometry: BaseGeometry) -> BaseGeometry:
    """Coerce a union/difference result back to Polygon/MultiPolygon.

    Live crash, «18. Кустанайская улица»: `_combine_with_holes()`'s
    `unary_union`/`difference` calls can hand back a `GeometryCollection`
    instead of a clean polygon — GEOS does this when two pieces touch along a
    degenerate lower-dimensional edge (a shared boundary segment, a
    near-zero-width sliver from floating-point noise) and folds a spurious
    `LineString`/`Point` component in alongside the real area. Nothing here
    downstream expects that: `patterns.py::territory_guide()` calls
    `.boundary` on whatever `territory_polygon()` returns to build the
    along-the-edge row guide, and `GeometryCollection.boundary` is `None` in
    shapely (not an exception) — that `None` silently became a
    `RowGuide(geometry=None, ...)`, and the real crash surfaced several calls
    later and several files away, in `_row_path()`'s `guide.geometry.buffer(...)`,
    as a bare `AttributeError: 'NoneType' object has no attribute 'buffer'`.

    The fix applies the same principle `territory_polygon()` already applies
    to its own *input* zones (ignore what is not areal) to its own *output*:
    keep only the Polygon/MultiPolygon parts of the collection — the real
    area is always still in there, the degenerate parts contribute ~0 area
    and exist only as a GEOS book-keeping artifact of the operation, not
    because a genuine loss of site coverage happened.
    """
    if geometry.geom_type != "GeometryCollection":
        return geometry
    polygonal = [g for g in geometry.geoms if g.geom_type in _AREAL and not g.is_empty]
    if not polygonal:
        return geometry
    return polygonal[0] if len(polygonal) == 1 else unary_union(polygonal)


# Насколько близкими должны быть площади двух контуров, чтобы считать их одним
# и тем же. Не ноль: один и тот же контур, пришедший из разных файлов бандла,
# может отличаться в последних знаках после конвертации DWG->DXF.
_DUPLICATE_AREA_TOLERANCE = 1e-6


def _without_duplicates(pieces: list[BaseGeometry]) -> list[BaseGeometry]:
    """Убрать повторы одного и того же контура.

    Живой отказ на «4. Харьковская улица», и он тихий — самый опасный вид.
    Бандл ссылается на один чертёж четырежды, поэтому в `territory_polygon`
    приходят четыре копии каждого контура: {54 370, 863, 56, 0} м² по четыре
    штуки. Дубликат по определению содержится в уже собранном результате на
    100 %, логика дырок принимает его за внутреннее кольцо и ВЫЧИТАЕТ только
    что добавленное; следующая копия добавляет обратно, и на чётном числе
    копий остаётся ровно ноль. Итог: `territory_polygon` возвращала полигон
    площадью 0 м², план выходил пустым, а CLI при этом рапортовал «Готово» с
    кодом возврата 0 — то есть пользователь получал молча неверный результат,
    а не ошибку.

    Сравнение по площади и габариту, а потом `equals` — порядок не случайный:
    `equals` топологическое и дорогое, а дешёвый ключ отсекает почти всё до
    него. Контуров тут единицы-десятки, но на них же и строится вся
    территория, так что перестраховка дешевле ошибки.
    """
    unique: list[BaseGeometry] = []
    for piece in pieces:
        key = (round(piece.area, 6), tuple(round(v, 6) for v in piece.bounds))
        if any(
            key == (round(seen.area, 6), tuple(round(v, 6) for v in seen.bounds)) and piece.equals(seen)
            for seen in unique
        ):
            continue
        unique.append(piece)
    return unique


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
    by_area_desc = _without_duplicates(sorted(pieces, key=lambda g: -g.area))
    result = by_area_desc[0]
    for piece in by_area_desc[1:]:
        if piece.area <= 0:
            continue
        contained_fraction = piece.intersection(result).area / piece.area
        if contained_fraction > 0.99:
            result = _as_polygonal(result.difference(piece))
        else:
            result = _as_polygonal(unary_union([result, piece]))
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
