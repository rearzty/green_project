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

    def test_a_zone_fully_inside_another_is_cut_out_as_a_hole_not_unioned(self):
        """Живая находка, «1. Олимпийская деревня»: тот же слой «Границы
        работ» несёт и широкую внешнюю границу, и несколько отдельно
        нарисованных, гораздо более мелких колец, каждое на 100% внутри
        внешнего — это исключаемые из работы куски (сверено с пользователем
        напрямую по чертежу), а не дополнительная площадь. Обычное
        объединение здесь no-op (маленькое кольцо не высовывается за
        границы большого), поэтому нужно явное вычитание, а не просто
        unary_union."""
        from shapely.geometry import Polygon

        from backend.app.services.pipeline_service import territory_polygon
        from geo_engine.model import Zone

        outer = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
        hole = Polygon([(20, 20), (40, 20), (40, 40), (20, 40)])
        zones = [Zone(geometry=outer, zone_type="territory"), Zone(geometry=hole, zone_type="territory")]

        territory = territory_polygon(zones)

        assert territory.area == pytest.approx(outer.area - hole.area)
        assert territory.intersection(hole).area == pytest.approx(0.0, abs=1e-9)

    def test_an_island_inside_a_hole_is_added_back(self):
        """Дыра внутри дыры -- островок посередине исключённого куска,
        который сам всё же входит в объём работ. Тот же класс геометрии,
        что у полигона с отверстиями и островами в ГИС в целом, не
        специальный случай под одну улицу."""
        from shapely.geometry import Polygon

        from backend.app.services.pipeline_service import territory_polygon
        from geo_engine.model import Zone

        outer = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
        hole = Polygon([(20, 20), (60, 20), (60, 60), (20, 60)])
        island = Polygon([(30, 30), (40, 30), (40, 40), (30, 40)])
        zones = [
            Zone(geometry=outer, zone_type="territory"),
            Zone(geometry=hole, zone_type="territory"),
            Zone(geometry=island, zone_type="territory"),
        ]

        territory = territory_polygon(zones)

        assert territory.area == pytest.approx(outer.area - hole.area + island.area)
        assert territory.intersection(island).area == pytest.approx(island.area)


class TestAsPolygonal:
    """`_as_polygonal()` -- the fix for a live crash on «18. Кустанайская
    улица»: `_combine_with_holes()`'s `unary_union`/`difference` calls
    occasionally hand back a `GeometryCollection` (GEOS folding a degenerate
    LineString/Point sliver in alongside the real area) instead of a clean
    polygon, and `GeometryCollection.boundary` is `None` in shapely -- not an
    exception -- which crashed `patterns.py::territory_guide()` several calls
    downstream with a bare `AttributeError` on `None.buffer(...)`.

    Reproducing the exact GEOS floating-point trigger synthetically (adjacent-
    square union, identical- and near-identical-polygon difference) wasn't
    found to be practical, so this tests the fix function directly against a
    hand-built collection of the same shape GEOS is known to produce on real
    data, plus the `.boundary is None` mechanism itself, confirmed directly
    against shapely.
    """

    def test_a_line_fragment_is_dropped_and_the_polygon_kept(self):
        from shapely.geometry import GeometryCollection, LineString, Polygon

        from geo_engine.territory import _as_polygonal

        polygon = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
        collection = GeometryCollection([polygon, LineString([(0, 0), (1, 1)])])
        assert collection.boundary is None  # the actual crash mechanism this guards against

        result = _as_polygonal(collection)

        assert result.geom_type == "Polygon"
        assert result.area == polygon.area
        assert result.boundary is not None

    def test_multiple_polygonal_parts_are_unioned(self):
        from shapely.geometry import GeometryCollection, Point, Polygon

        from geo_engine.territory import _as_polygonal

        north = Polygon([(0, 100), (100, 100), (100, 200), (0, 200)])
        south = Polygon([(0, 0), (100, 0), (100, 50), (0, 50)])
        collection = GeometryCollection([north, south, Point(500, 500)])

        result = _as_polygonal(collection)

        assert result.geom_type == "MultiPolygon"
        assert result.area == pytest.approx(north.area + south.area)

    def test_a_non_collection_geometry_passes_through_unchanged(self):
        from shapely.geometry import Polygon

        from geo_engine.territory import _as_polygonal

        polygon = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
        assert _as_polygonal(polygon) is polygon

    def test_an_all_degenerate_collection_is_returned_as_is(self):
        """Should never happen (the real area can't vanish), but if it did,
        returning the input as-is is safer than raising from inside a helper
        two modules away from the actual data."""
        from shapely.geometry import GeometryCollection, LineString, Point

        from geo_engine.territory import _as_polygonal

        collection = GeometryCollection([LineString([(0, 0), (1, 1)]), Point(5, 5)])

        result = _as_polygonal(collection)

        assert result is collection


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

    def test_a_distant_contour_is_kept_if_real_infrastructure_surrounds_it(self):
        """13. Харьковский проезд: один заказ на съёмку («output[1-6]_...»,
        шесть блоков) честно распадается на два кластера ~4.5 км друг от
        друга — то же расстояние, что и у настоящего мусора в
        test_a_contour_kilometres_away_is_dropped, так что дистанция одна
        здесь не разделяющий признак. Разделяет то, что рядом: у мусорного
        куска на 4. Харьковской в радиусе 100м — 0 сетей; у обоих кластеров
        харьковского проезда — 12+ сетей и тысячи зон.
        """
        from shapely.geometry import LineString, Polygon

        from geo_engine.territory import territory_polygon
        from geo_engine.model import Utility, Zone

        site = Polygon([(0, 0), (200, 0), (200, 200), (0, 200)])
        far_site = Polygon([(5000, 0), (5100, 0), (5100, 100), (5000, 100)])
        zones = [
            Zone(geometry=site, zone_type="territory"),
            Zone(geometry=far_site, zone_type="territory"),
        ]
        # Enough utilities near far_site to look like real infrastructure,
        # not a stray CRS artifact -- mirrors the live count (12-178 utilities)
        # seen on the genuinely-real far cluster, well above the 0 seen on
        # the actually-bogus one.
        utilities = [Utility(geometry=LineString([(5000 + i, 10), (5000 + i, 90)]), object_type="cable_line") for i in range(6)]

        territory = territory_polygon(zones, utilities)

        assert territory.area == pytest.approx(site.area + far_site.area)

    def test_a_distant_contour_with_no_nearby_infrastructure_is_still_dropped(self):
        """Same distance as the rescue case above, but nothing real nearby --
        the Измайловская-shaped bug this clustering exists to catch in the
        first place. Utilities existing elsewhere in the bundle must not
        rescue a piece they are nowhere near.
        """
        from shapely.geometry import LineString, Polygon

        from geo_engine.territory import territory_polygon
        from geo_engine.model import Utility, Zone

        site = Polygon([(0, 0), (200, 0), (200, 200), (0, 200)])
        far_site = Polygon([(5000, 0), (5100, 0), (5100, 100), (5000, 100)])
        zones = [
            Zone(geometry=site, zone_type="territory"),
            Zone(geometry=far_site, zone_type="territory"),
        ]
        utilities = [Utility(geometry=LineString([(10, 10), (190, 190)]), object_type="cable_line")]

        territory = territory_polygon(zones, utilities)

        assert territory.geom_type == "Polygon"
        assert territory.area == pytest.approx(site.area)
