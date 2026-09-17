"""Unit tests for pipeline_service.py's pure (non-DB) functions:
_find_reusable_plan's reuse-vs-regenerate decision, and _effective_norms'
per-generation spacing override.
"""

from __future__ import annotations

import pytest

from backend.app.db.models import Plan, Project
from backend.app.services.pipeline_service import CurrentPlanDeletionError, _effective_norms, _find_reusable_plan, delete_plan
from geo_engine.norms import load_norms

NORMS = load_norms()


def _plan(
    tree_spacing_m: float | None = None,
    shrub_spacing_m: float | None = None,
    scoring_mode: str = "heuristic",
    planting_types: list[str] | None = None,
    is_current: bool = True,
    has_manual_edits: bool = False,
) -> Plan:
    return Plan(
        id="plan-1",
        project_id="project-1",
        scoring_mode=scoring_mode,
        planting_types=planting_types or ["tree", "shrub"],
        tree_spacing_m=tree_spacing_m,
        shrub_spacing_m=shrub_spacing_m,
        is_current=is_current,
        has_manual_edits=has_manual_edits,
    )


def _project_with(plan: Plan) -> Project:
    project = Project(id="project-1", name="test")
    project.plans = [plan]
    return project


class TestFindReusablePlanWithSpacing:
    def test_reuses_when_spacing_matches_exactly(self):
        project = _project_with(_plan(tree_spacing_m=6.0, shrub_spacing_m=None))
        result = _find_reusable_plan(project, "heuristic", ["tree", "shrub"], 6.0, None)
        assert result is project.plans[0]

    def test_reuses_when_neither_request_overrides_spacing(self):
        project = _project_with(_plan())
        result = _find_reusable_plan(project, "heuristic", ["tree", "shrub"], None, None)
        assert result is project.plans[0]

    def test_does_not_reuse_when_tree_spacing_differs(self):
        """The bug this guards: before tree_spacing_m/shrub_spacing_m were
        part of the comparison, changing only the interval while keeping the
        same scoring_mode/planting_types would silently hand back the old
        plan instead of regenerating with the new spacing."""
        project = _project_with(_plan(tree_spacing_m=5.0))
        result = _find_reusable_plan(project, "heuristic", ["tree", "shrub"], 8.0, None)
        assert result is None

    def test_does_not_reuse_when_shrub_spacing_differs(self):
        project = _project_with(_plan(shrub_spacing_m=3.0))
        result = _find_reusable_plan(project, "heuristic", ["tree", "shrub"], None, 4.0)
        assert result is None

    def test_does_not_reuse_when_request_clears_a_previously_set_override(self):
        project = _project_with(_plan(tree_spacing_m=6.0))
        result = _find_reusable_plan(project, "heuristic", ["tree", "shrub"], None, None)
        assert result is None


class TestDeletePlan:
    def test_refuses_to_delete_the_current_plan(self):
        project = _project_with(_plan(is_current=True))
        with pytest.raises(CurrentPlanDeletionError):
            delete_plan(project, project.plans[0])

    def test_removes_a_non_current_plan_from_the_project(self):
        current = _plan(is_current=True)
        current.id = "plan-current"
        old = _plan(is_current=False)
        old.id = "plan-old"
        project = _project_with(current)
        project.plans.append(old)

        delete_plan(project, old)

        assert [p.id for p in project.plans] == ["plan-current"]


class TestEffectiveNorms:
    def test_returns_unmodified_norms_when_nothing_overridden(self):
        assert _effective_norms(NORMS, None, None) is NORMS

    def test_applies_tree_override_only(self):
        effective = _effective_norms(NORMS, 8.0, None)
        assert effective.spacing_for("tree").min_distance_m == 8.0
        assert effective.spacing_for("shrub") == NORMS.spacing_for("shrub")

    def test_applies_both_overrides(self):
        effective = _effective_norms(NORMS, 8.0, 4.0)
        assert effective.spacing_for("tree").min_distance_m == 8.0
        assert effective.spacing_for("shrub").min_distance_m == 4.0


class TestTerritoryPolygon:
    """Selecting the site outline out of whatever the drawings labelled
    "territory". Both cases below are the real pilot data, not hypotheticals:
    the work area there is two polygons, and the same layer in the main drawing
    also carries leftover fragments that are not areas at all.
    """

    def test_multiple_territory_zones_are_unioned_not_picked_from(self):
        from shapely.geometry import Polygon

        from backend.app.services.pipeline_service import territory_polygon
        from geo_engine.model import Zone

        north = Polygon([(0, 100), (100, 100), (100, 200), (0, 200)])
        south = Polygon([(0, 0), (100, 0), (100, 50), (0, 50)])
        zones = [Zone(geometry=north, zone_type="territory"), Zone(geometry=south, zone_type="territory")]

        territory = territory_polygon(zones)

        assert territory.area == north.area + south.area

    def test_fragment_linestrings_on_the_territory_layer_are_ignored(self):
        from shapely.geometry import LineString, Polygon

        from backend.app.services.pipeline_service import territory_polygon
        from geo_engine.model import Zone

        fragment = LineString([(5, 5), (6, 6)])
        outline = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
        # Fragment first: picking the first match would return the LineString.
        zones = [Zone(geometry=fragment, zone_type="territory"), Zone(geometry=outline, zone_type="territory")]

        territory = territory_polygon(zones)

        assert territory.geom_type == "Polygon"
        assert territory.area == outline.area

    def test_only_fragments_still_raises(self):
        from shapely.geometry import LineString

        from backend.app.services.pipeline_service import MissingTerritoryError, territory_polygon
        from geo_engine.model import Zone

        zones = [Zone(geometry=LineString([(0, 0), (1, 1)]), zone_type="territory")]

        with pytest.raises(MissingTerritoryError):
            territory_polygon(zones)


class TestTerritoryClustering:
    """Отбрасывание контуров, которые участку не принадлежат.

    Бандл склеивается конкатенацией, и это верно ровно до тех пор, пока все
    файлы в одной системе координат. В пилотных данных нашёлся файл внешней
    ссылки в другой: его контур объединялся с настоящим, `territory_polygon`
    возвращала два куска в трёх километрах друг от друга, и половина посадок
    уезжала туда, где нет ни одной сети. План при этом считался полностью
    соответствующим нормативам — ближайшее ограничение было за три километра.
    Тихий неверный результат, на глаз неотличимый от верного.
    """

    def test_a_contour_kilometres_away_is_dropped(self):
        from shapely.geometry import Polygon

        from geo_engine.territory import territory_polygon
        from geo_engine.model import Zone

        site = Polygon([(17300, 13900), (17960, 13900), (17960, 14280), (17300, 14280)])
        elsewhere = Polygon([(14345, 11780), (14690, 11780), (14690, 11990), (14345, 11990)])
        zones = [
            Zone(geometry=site, zone_type="territory"),
            Zone(geometry=elsewhere, zone_type="territory"),
        ]

        territory = territory_polygon(zones)

        assert territory.geom_type == "Polygon"
        assert territory.area == pytest.approx(site.area)

    def test_adjacent_parts_of_one_site_are_kept(self):
        """Участок эталонной улицы физически состоит из двух полигонов — улица
        разрывается вокруг квартала. Их терять нельзя.
        """
        from shapely.geometry import Polygon

        from geo_engine.territory import territory_polygon
        from geo_engine.model import Zone

        north = Polygon([(0, 100), (200, 100), (200, 300), (0, 300)])
        south = Polygon([(0, 0), (200, 0), (200, 90), (0, 90)])
        zones = [Zone(geometry=north, zone_type="territory"), Zone(geometry=south, zone_type="territory")]

        territory = territory_polygon(zones)

        assert territory.area == pytest.approx(north.area + south.area)

    def test_the_largest_contour_anchors_the_cluster(self):
        """Если в данных смешаны два объекта, больший почти наверняка и есть
        заказанный участок — а не тот, что оказался первым в списке.
        """
        from shapely.geometry import Polygon

        from geo_engine.territory import territory_polygon
        from geo_engine.model import Zone

        small_stray = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
        real_site = Polygon([(5000, 5000), (5400, 5000), (5400, 5300), (5000, 5300)])
        zones = [
            Zone(geometry=small_stray, zone_type="territory"),
            Zone(geometry=real_site, zone_type="territory"),
        ]

        territory = territory_polygon(zones)

        assert territory.area == pytest.approx(real_site.area)
