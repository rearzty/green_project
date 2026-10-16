"""Build the union of setback buffers ("exclusion zone") that a planting of a
given type is not allowed to intersect, per geo_engine/config/planting_norms.yaml.
"""

from __future__ import annotations

import numpy as np
import shapely
from shapely import unary_union
from shapely.geometry.base import BaseGeometry

from geo_engine.model import PlantingType, Utility, Zone
from geo_engine.norms import PlantingNorms
from geo_engine.species import Species


# Shapely approximates a buffer's round caps/corners with a polygon of
# `quad_segs` segments per quarter-circle; the default (8) leaves a chordal
# gap of a couple millimeters at a setback of a few tenths of a meter (now
# that lawn's setbacks are 0.3-0.5m, not just tree/shrub's 1.5-5.0m, that gap
# is no longer negligible relative to the required distance). A higher
# resolution keeps the buffered boundary within ~0.1mm of the true circle at
# any setback distance we use.
_BUFFER_QUAD_SEGS = 32

# Насколько дальше границы участка зона отступов обязана совпадать с полной,
# когда её строят с `clip_to`. Внутри участка зону читают двумя способами:
# вычитание из площади (нужна только сама территория) и расстояние от
# кандидата до ближайшего ограничения для ранжирования — а этот признак
# насыщается на `ml_scoring.features.REFERENCE_CLEARANCE_M` (5 м): всё, что
# дальше, ранжированию безразлично. Запас взят вдвое против насыщения;
# соотношение сторожит тест, чтобы правка одной константы не сломала другую
# молча (geo_engine не может импортировать ml_scoring — зависимость обратная).
EXCLUSION_CLIP_MARGIN_M = 10.0

# Значение по умолчанию у `shapely.buffer(mitre_limit=...)`.
_MITRE_LIMIT = 5.0



def valid_polygonal_union(geometries: list[BaseGeometry]) -> BaseGeometry:
    """Объединить площадные геометрии, пережив невалидные среди них.

    Живой отказ на реальной улице («13. Харьковский проезд»): весь прогон
    падал с `TopologyException: side location conflict`, и падал целиком —
    отчёта не было вообще. Замер по этой улице: твёрдых препятствий 921, из
    них невалидных **7 (0,76 %)**, все с диагнозом `Self-intersection`. То
    есть семь самопересекающихся контуров из девятисот уносили улицу.

    Откуда они берутся: обмерные чертежи рисуют здания и покрытия
    полилиниями, которые сходятся не идеально, а
    `geometry_cleanup.reconstruct_closed_footprints()` собирает из них
    полигоны — на честно кривом входе часть колец получается с
    самопересечением. GEOS считать оверлей на таком входе не обязан.

    Чинится точечно: `shapely.is_valid` по всему массиву дёшево (это C-цикл),
    а `make_valid` вызывается ТОЛЬКО для невалидных. На той же улице union
    после этого проходит мгновенно (0,0 с). Глобальный `make_valid` по всем
    объектам был бы дороже на порядки — на реальных улицах их сотни тысяч.

    После починки остаются только площадные части. Это не придирка:
    `make_valid` разворачивает самопересекающийся полигон в
    `GeometryCollection` с линиями внутри, а смешанная размерность второго
    операнда `difference()` — ровно тот краш, который master уже чинил
    отдельно (17. Грузинская М ул, «cannot determine overlay result geometry
    dimension»). Вернуть его назад этой правкой было бы обидно.
    """
    if not geometries:
        return unary_union([])
    array = np.array(geometries, dtype=object)
    healthy = shapely.is_valid(array)
    if not healthy.all():
        array = np.where(healthy, array, shapely.make_valid(array))
    parts: list[BaseGeometry] = []
    for geometry in array:
        if geometry is None or geometry.is_empty:
            continue
        if geometry.geom_type in ("Polygon", "MultiPolygon"):
            parts.append(geometry)
        elif geometry.geom_type == "GeometryCollection":
            parts.extend(g for g in geometry.geoms if g.geom_type in ("Polygon", "MultiPolygon"))
    return unary_union(parts) if parts else unary_union([])


def repaired_for_buffering(geometries: list[BaseGeometry]) -> list[BaseGeometry]:
    """Починить невалидные геометрии перед буферизацией.

    Живой отказ на «1. Олимпийская деревня», и он молчаливый — хуже
    падения. Сирень встала в **0,76 м от водопровода** при требуемом метре:
    генератор считал место законным, а проверка мерила расстояние до
    исходной геометрии и видела нарушение. То есть план, который отвергает
    собственная проверка сервиса.

    Причина: ближайшая труба пришла из чертежа САМОПЕРЕСЕКАЮЩИМСЯ полигоном
    (`Self-intersection`), а `buffer()` на невалидном входе не падает — он
    возвращает неверную фигуру. Проверено прямо на этой трубе: её
    собственный буфер в 1,0 м точку НЕ накрывал, после `make_valid` —
    накрывает. В зоне отступов получалась дыра, которой в данных нет.

    Масштаб больше одной посадки: на этой улице невалидны **763 сети из
    93 715 (0,81 %)** — 512 канализаций, 181 кабель, 53 водопровода, 17
    теплосетей. У каждой охранная зона считалась по сломанной фигуре;
    заметной нарушением стала одна, потому что остальные дыры пришлись на
    места, куда посадки и так не встали.

    Тот же класс, что и `valid_polygonal_union()` ниже, но там невалидный
    вход ронял оверлей с `TopologyException`, а здесь — тихо искажает
    результат. Разница в поведении, не в причине.

    Площадные части здесь НЕ выделяются, в отличие от
    `valid_polygonal_union()`: сеть — это чаще всего линия, и буфер вокруг
    линии и есть её охранная зона. Отбросить линейное значило бы потерять
    почти все сети разом.
    """
    if not geometries:
        return []
    array = np.array(geometries, dtype=object)
    healthy = shapely.is_valid(array)
    if healthy.all():
        return geometries
    return list(np.where(healthy, array, shapely.make_valid(array)))


def build_exclusion_zone(
    utilities: list[Utility],
    zones: list[Zone],
    planting_type: PlantingType,
    norms: PlantingNorms,
    species: Species | None = None,
    crown_reference_diameter_m: float = 5.0,
    clip_to: BaseGeometry | None = None,
) -> BaseGeometry:
    """Union of every utility/zone buffered by its required setback for
    `planting_type`. Zone types with a 0.0 setback (e.g. lawn vs. cables)
    still buffer by 0, which is a geometric no-op but keeps the logic uniform.

    `species` matters and must be the same one the plan will actually use.
    After the Moscow acts were read, the required setback stopped being a
    single table value: МГСН 1.02-02 п. 4.2.8 sets per-species distances from
    heat mains, 743-ПП табл. 3.6.1 прим. 3 pushes a wide crown 10 m off a
    building, and прим. 1 to both tables scales the table values with crown
    diameter. Leaving those out here while `compliance.explain_items` applies
    them produces a plan the tool's own checker rejects — measured live on a
    real street: with a wide-crown species, 80 of 121 placements came back as
    violations purely because the generator had used the unadjusted table.

    `clip_to` — участок, ради которого строится зона. Съёмка почти всегда
    шире участка в разы, а итоговый `unary_union` по всей съёмке был самой
    дорогой операцией генерации: на «4. Харьковская улица» 172 с из 238 у
    газона, при том что дальше от зоны используется только то, что рядом с
    участком. Поэтому в объединение идут лишь объекты, чей буфер подходит к
    участку ближе `EXCLUSION_CLIP_MARGIN_M` — внутри этой полосы зона
    совпадает с полной, а дальше её никто не читает (см. константу).

    «Совпадает» — с точностью до последних знаков: GEOS нодирует объединение
    меньшего набора чуть иначе. Замер на той же улице: все 2818 посадок
    прежнего плана на месте, добавилось одно дерево, стоящее ровно на норме
    (2,00 м от дороги при требуемых 2,00) — кандидат на самой границе зоны.
    Проверка его принимает; полный прогон 123 с → 92 с.
    """
    pending: list[tuple[BaseGeometry, float, str]] = []

    def required(object_type: str) -> float:
        return norms.resolve_setback(
            object_type, planting_type, species, crown_reference_diameter_m
        ).required_m

    # Геометрия чинится ДО буферизации, а не после: `buffer()` на невалидном
    # входе не падает, он молча отдаёт неверную фигуру — см.
    # `repaired_for_buffering`.
    for utility, geometry in zip(
        utilities, repaired_for_buffering([u.geometry for u in utilities])
    ):
        # Round join/cap: a utility is a line (possibly bent), and a rounded
        # clearance radius around a pipe/cable is physically the right shape.
        pending.append((geometry, required(utility.object_type), "round"))

    setback_zones = [z for z in zones if z.zone_type in norms.setbacks_m]
    for zone, zone_geometry in zip(
        setback_zones, repaired_for_buffering([z.geometry for z in setback_zones])
    ):
        # Mitre join: a zone (building, road) is rectilinear, and offsetting
        # it should stay rectilinear too -- shapely's default round join
        # rounds every corner, which for e.g. a building turns a rectangular
        # setback strip into a blob-cornered shape (visibly wrong on the map)
        # and bloats the corner into ~30 extra vertices for no reason.
        pending.append((zone_geometry, required(zone.zone_type), "mitre"))

    if clip_to is not None and pending and not clip_to.is_empty:
        geometries = np.array([g for g, _, _ in pending], dtype=object)
        # Острый угол буфера с `mitre` выступает дальше самого отступа — до
        # `mitre_limit` (у shapely 5.0) отступов от вершины. Дальность отбора
        # обязана это покрывать, иначе потерялся бы выступ, который реально
        # дотягивается до участка.
        reach = np.array(
            [setback * (_MITRE_LIMIT if join_style == "mitre" else 1.0) for _, setback, join_style in pending]
        ) + EXCLUSION_CLIP_MARGIN_M
        shapely.prepare(clip_to)
        near = shapely.dwithin(geometries, clip_to, reach)
        pending = [entry for entry, keep in zip(pending, near) if keep]

    if not pending:
        # No constraints at all -> empty exclusion zone (everything is candidate territory).
        return unary_union([])

    return unary_union(
        [
            geometry.buffer(setback, quad_segs=_BUFFER_QUAD_SEGS, join_style=join_style)
            for geometry, setback, join_style in pending
        ]
    )


# "sidewalk" joined this list once real Polygon geometry for it existed to
# subtract at all -- historically the layer was always a LineString (a
# pedestrian-path *edge*, same shape as road's kerb), so the hard-obstacle
# filter below (Polygon/MultiPolygon only) silently skipped it and only the
# setback buffer (0.7/0.5/0.0 m) applied. Live case, 4. Харьковская улица:
# "ДВ_ПП_ДО_ТипN_..." pavement-repair-scope layers (see layer_rules.py)
# include real sidewalk-surface polygons -- a candidate sitting 1m from one
# edge of a 2m+ wide sidewalk is still standing on paved sidewalk, not just
# too close to its edge, and the setback alone can't express that (same
# reasoning as road/building here). Adding it here is a pure no-op for any
# project where sidewalk is still only ever a line, like before.
HARD_OBSTACLE_ZONE_TYPES = ("building", "road", "existing_greenery", "sidewalk")


def buildable_area(
    territory: BaseGeometry, exclusion_zone: BaseGeometry, other_zones: list[Zone], territory_margin_m: float = 0.0
) -> BaseGeometry:
    """Territory minus the exclusion zone minus any hard obstacles (buildings,
    existing pavement/roads, already-planted greenery) that are never
    plantable regardless of setback — unlike the setback buffers, these are
    subtracted as their exact footprint, not grown by a distance. Otherwise
    a candidate could land directly inside e.g. an existing tree/shrub bed;
    `ml_scoring` layering `existing_greenery_gap_score` on top only ranks
    candidates that are already legal, it doesn't make them legal.

    `territory_margin_m` (planting_norms.yaml's territory_margin_m, per
    planting_type) erodes the territory's own outer boundary inward before
    anything else is subtracted — a genuinely different operation from every
    other setback here: those grow an *obstacle* outward and subtract the
    result, which can't express "keep back from the property line" (there's
    no obstacle geometry to grow, the boundary itself is the constraint).
    Without this, a candidate could land right at the edge of the plot --
    found live on real data.
    """
    if territory_margin_m > 0:
        territory = territory.buffer(-territory_margin_m)
    # Only polygonal geometry can subtract area in the first place -- a
    # building/road read as an unclosed LineString (real Мосгеотрест data)
    # or a stray Point already contributes nothing to a
    # difference against a polygon. Live crash on real data (17. Грузинская
    # М ул): thousands of such LineStrings/Points mixed into this same list
    # made `unary_union(hard_obstacles)` a heterogeneous GeometryCollection,
    # and GEOS's overlay engine cannot always compute a result dimension for
    # a mixed-dimension second operand of `difference()`
    # ("AssertionFailedException: ... determine overlay result geometry
    # dimension") -- dropping the zero-area geometries here is a no-op for
    # the result and removes the crash at the source, rather than papering
    # over it with a repair/retry on the union.
    hard_obstacles = [
        z.geometry
        for z in other_zones
        if z.zone_type in HARD_OBSTACLE_ZONE_TYPES and z.geometry.geom_type in ("Polygon", "MultiPolygon")
    ]
    if exclusion_zone is not None and not exclusion_zone.is_empty:
        result = territory.difference(exclusion_zone)
    else:
        result = territory
    if hard_obstacles:
        result = result.difference(valid_polygonal_union(hard_obstacles))
    return result
