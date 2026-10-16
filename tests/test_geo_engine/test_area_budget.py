"""Объяснение «почему мало посадок»: доли площади, закрытые ограничениями."""

from __future__ import annotations

from shapely.geometry import LineString, box

from geo_engine.area_budget import area_budget
from geo_engine.buffers import build_exclusion_zone, buildable_area
from geo_engine.model import Utility, Zone
from geo_engine.norms import load_norms

NORMS = load_norms()


def _street():
    """Лента 200×14 м, посередине проезжая часть, вдоль неё кабель —
    как на «17. Грузинская»: деревьям места почти нет, кустарнику есть."""
    territory = box(0, 0, 200, 14)
    zones = [Zone(geometry=territory, zone_type="territory"), Zone(geometry=LineString([(0, 7), (200, 7)]), zone_type="road")]
    utilities = [Utility(geometry=LineString([(0, 4), (200, 4)]), object_type="cable_line")]
    return territory, utilities, zones


def test_the_dominant_blocker_is_named_first():
    territory, utilities, zones = _street()
    budget = area_budget(territory, utilities, zones, "tree", NORMS)
    assert budget.blockers[0].object_type in ("road", "territory_margin")
    assert {b.object_type for b in budget.blockers} >= {"road", "cable_line"}
    assert "закрывают" in budget.summary_ru()


def test_usable_area_matches_the_exact_geometry():
    territory, utilities, zones = _street()
    for planting_type in ("tree", "shrub"):
        budget = area_budget(territory, utilities, zones, planting_type, NORMS)
        exact = buildable_area(
            territory,
            build_exclusion_zone(utilities, zones, planting_type, NORMS),
            zones,
            territory_margin_m=NORMS.territory_margin_for(planting_type),
        ).area
        assert abs(budget.usable_m2 - exact) <= 0.03 * territory.area


def test_shrubs_get_more_room_than_trees_on_a_dense_street():
    territory, utilities, zones = _street()
    tree = area_budget(territory, utilities, zones, "tree", NORMS)
    shrub = area_budget(territory, utilities, zones, "shrub", NORMS)
    assert shrub.usable_m2 > tree.usable_m2
