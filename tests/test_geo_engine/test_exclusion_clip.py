"""Зона отступов, обрезанная по участку, обязана вести себя как полная.

`build_exclusion_zone(clip_to=...)` выкидывает из объединения объекты съёмки,
далёкие от участка, — ради скорости (union по всей съёмке был самой дорогой
операцией генерации). Это корректно, только пока внутри участка и в полосе
`EXCLUSION_CLIP_MARGIN_M` вокруг него зона совпадает с полной, поэтому
проверяется именно это, а не «стало быстрее».
"""

from __future__ import annotations

from shapely.geometry import LineString, Point, Polygon, box

from geo_engine.buffers import EXCLUSION_CLIP_MARGIN_M, build_exclusion_zone, buildable_area
from geo_engine.model import Utility, Zone
from geo_engine.norms import load_norms
from ml_scoring.features import REFERENCE_CLEARANCE_M

NORMS = load_norms()
TERRITORY = box(0, 0, 100, 80)


def _scene():
    utilities = [
        Utility(geometry=LineString([(-20, 40), (120, 40)]), object_type="heat_network"),
        Utility(geometry=LineString([(-5, -50), (-5, 130)]), object_type="gas_pipe"),  # снаружи, но буфер дотягивается
        Utility(geometry=LineString([(500, 500), (900, 500)]), object_type="water_pipe"),  # далеко
    ]
    zones = [
        Zone(geometry=box(20, 10, 35, 25), zone_type="building"),
        Zone(geometry=box(2000, 2000, 2100, 2100), zone_type="building"),  # далеко
        # Острый угол: буфер с mitre выступает дальше самого отступа, и этот
        # выступ доходит до участка, хотя сам контур от него дальше отступа.
        Zone(geometry=Polygon([(-30, 90), (-8, 86), (-30, 88)]), zone_type="building"),
        Zone(geometry=TERRITORY, zone_type="territory"),
    ]
    return utilities, zones


def test_buildable_area_is_identical_with_and_without_clipping():
    utilities, zones = _scene()
    for planting_type in ("tree", "shrub", "lawn"):
        full = build_exclusion_zone(utilities, zones, planting_type, NORMS)
        clipped = build_exclusion_zone(utilities, zones, planting_type, NORMS, clip_to=TERRITORY)
        a = buildable_area(TERRITORY, full, zones)
        b = buildable_area(TERRITORY, clipped, zones)
        assert a.symmetric_difference(b).area < 1e-6, planting_type


def test_clipped_zone_matches_the_full_one_within_the_margin_band():
    utilities, zones = _scene()
    full = build_exclusion_zone(utilities, zones, "tree", NORMS)
    clipped = build_exclusion_zone(utilities, zones, "tree", NORMS, clip_to=TERRITORY)
    band = TERRITORY.buffer(EXCLUSION_CLIP_MARGIN_M)
    assert full.intersection(band).symmetric_difference(clipped.intersection(band)).area < 1e-6


def test_far_away_objects_are_actually_dropped():
    utilities, zones = _scene()
    clipped = build_exclusion_zone(utilities, zones, "tree", NORMS, clip_to=TERRITORY)
    assert not clipped.intersects(Point(700, 500))
    assert not clipped.intersects(Point(2050, 2050))


def test_margin_covers_the_point_where_ranking_stops_caring_about_clearance():
    """За `REFERENCE_CLEARANCE_M` признак запаса по отступу насыщается, и всё,
    что дальше, ранжированию безразлично. Если запас обрезки станет меньше,
    дальняя (выброшенная) часть зоны могла бы оказаться ближайшей к
    кандидату — и план тихо поменялся бы."""
    assert EXCLUSION_CLIP_MARGIN_M > REFERENCE_CLEARANCE_M
