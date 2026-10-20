"""Существующие деревья, снятые точкой, мешают посадке.

Раньше `buildable_area()` вычитала только площадные твёрдые препятствия
(фильтр по Polygon), а отдельно стоящее дерево съёмки — точка, то есть
молча ничего не вычитало: новая посадка могла встать прямо на него.
"""

import pytest
from shapely.geometry import Point, box

from geo_engine.area_budget import HARD_OBSTACLES_KEY, _blocker_masks
from geo_engine.buffers import buildable_area, point_obstacle_radius_m
from geo_engine.model import Zone
from geo_engine.norms import load_norms
from geo_engine.planner import plan_items

NORMS = load_norms()
TERRITORY = box(0, 0, 60, 60)


def _zones(*trees: Point) -> list[Zone]:
    return [Zone(geometry=TERRITORY, zone_type="territory")] + [
        Zone(geometry=tree, zone_type="existing_greenery") for tree in trees
    ]


def test_a_point_tree_removes_a_disc_of_the_new_crown_radius_from_the_buildable_area():
    tree = Point(30, 30)
    radius = point_obstacle_radius_m(NORMS, "tree")

    area = buildable_area(TERRITORY, None, _zones(tree), point_obstacle_radius_m=radius)

    assert not area.intersects(Point(30, 30))
    assert not area.intersects(Point(30 + radius * 0.95, 30))
    assert area.contains(Point(30 + radius * 1.05, 30))
    assert area.area == pytest.approx(TERRITORY.area - 3.14159 * radius**2, rel=0.01)


def test_without_a_radius_points_still_subtract_nothing():
    # Прежнее поведение сохранено для вызывающих, которым радиус кроны
    # неизвестен (обучение скоринга) — отказ от него явный, не случайный.
    area = buildable_area(TERRITORY, None, _zones(Point(30, 30)))
    assert area.equals(TERRITORY)


def test_a_lawn_may_cover_an_existing_tree():
    # Дерево на газоне — норма (CLAUDE.md, «дерево поверх газона»).
    assert point_obstacle_radius_m(NORMS, "lawn") == 0.0
    assert point_obstacle_radius_m(NORMS, "tree") > 0
    assert point_obstacle_radius_m(NORMS, "shrub") > 0


def test_a_multipoint_survey_object_is_handled_too():
    from shapely.geometry import MultiPoint

    zones = [
        Zone(geometry=TERRITORY, zone_type="territory"),
        Zone(geometry=MultiPoint([(10, 10), (50, 50)]), zone_type="existing_greenery"),
    ]
    # Каждая точка MultiPoint — своё дерево: вычитаются оба круга.
    area = buildable_area(TERRITORY, None, zones, point_obstacle_radius_m=2.5)
    assert not area.intersects(Point(10, 10))
    assert not area.intersects(Point(50, 50))
    assert area.area == pytest.approx(TERRITORY.area - 2 * 3.14159 * 2.5**2, rel=0.01)


def test_no_planted_tree_lands_on_an_existing_point_tree_end_to_end():
    existing = [Point(15, 15), Point(30, 30), Point(45, 15), Point(30, 45)]
    zones = _zones(*existing)

    for planting_type in ("tree", "shrub"):
        items = plan_items(
            "point-obstacles", [], zones, TERRITORY, [planting_type],
            lambda candidates: [(0.5, "test") for _ in candidates], NORMS,
        )
        assert items, planting_type
        radius = point_obstacle_radius_m(NORMS, planting_type)
        for item in items:
            for tree in existing:
                assert item.geometry.distance(tree) >= radius - 1e-3, (planting_type, item.geometry, tree)


def test_the_area_budget_counts_the_same_blockage_as_the_planner():
    # Слой «Где можно сажать» и сам план обязаны закрывать одно и то же.
    import shapely

    zones = _zones(Point(30, 30))
    radius = point_obstacle_radius_m(NORMS, "tree")
    points = shapely.points([30.0, 30.0 + radius * 0.5, 30.0 + radius * 1.5, 5.0], [30.0, 30.0, 30.0, 5.0])

    masks = _blocker_masks(points, TERRITORY, [], zones, "tree", NORMS, None, 5.0)

    assert masks.masks[HARD_OBSTACLES_KEY].tolist() == [True, True, False, False]


class TestExplainRejection:
    """Причина отказа в конкретной точке — с актом и пунктом там, где они есть."""

    def _gas_pipe(self):
        from shapely.geometry import LineString

        from geo_engine.model import Utility

        return Utility(geometry=LineString([(0, 30), (60, 30)]), object_type="gas_pipe")

    def test_a_setback_violation_cites_the_act_and_the_clause(self):
        from geo_engine.compliance import explain_rejection

        reasons = explain_rejection(
            Point(30, 30.5), "tree", [self._gas_pipe()], _zones(), NORMS, territory=TERRITORY
        )

        setback = [r for r in reasons if r.kind == "setback"]
        assert len(setback) == 1
        assert setback[0].object_type == "gas_pipe"
        assert setback[0].required_m == pytest.approx(1.5)
        assert setback[0].actual_m == pytest.approx(0.5)
        # Ссылка не пустая и называет акт и пункт, а не «норматив».
        assert "п." in setback[0].citation

    def test_the_same_spot_is_accepted_once_it_is_far_enough(self):
        from geo_engine.compliance import explain_rejection

        assert explain_rejection(
            Point(30, 40), "tree", [self._gas_pipe()], _zones(), NORMS, territory=TERRITORY
        ) == []

    def test_an_existing_point_tree_is_a_reason_but_not_presented_as_a_norm(self):
        from geo_engine.compliance import explain_rejection

        reasons = explain_rejection(Point(30.5, 30), "tree", [], _zones(Point(30, 30)), NORMS, territory=TERRITORY)

        (reason,) = [r for r in reasons if r.kind == "existing_tree"]
        assert reason.verified is False
        assert "не норматив" in reason.citation.lower()

    def test_outside_and_too_close_to_the_property_line(self):
        from geo_engine.compliance import explain_rejection

        outside = explain_rejection(Point(-5, 30), "tree", [], _zones(), NORMS, territory=TERRITORY)
        assert [r.kind for r in outside] == ["outside_territory"]

        margin = NORMS.territory_margin_for("tree")
        near_line = explain_rejection(Point(margin / 2, 30), "tree", [], _zones(), NORMS, territory=TERRITORY)
        assert [r.kind for r in near_line] == ["territory_margin"]

    def test_every_reason_carries_a_citation(self):
        from geo_engine.compliance import explain_rejection

        reasons = explain_rejection(
            Point(30.2, 30.2), "tree", [self._gas_pipe()], _zones(Point(30, 30)), NORMS, territory=TERRITORY
        )
        assert len(reasons) >= 2
        assert all(r.citation for r in reasons)
