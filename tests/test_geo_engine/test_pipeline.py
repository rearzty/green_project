import pytest
from shapely import unary_union
from shapely.geometry import LineString, Point, box

from ml_scoring.heuristic_scorer import HeuristicScorer

from geo_engine.buffers import build_exclusion_zone, buildable_area
from geo_engine.candidates import generate_candidates
from geo_engine.model import Zone
from geo_engine.norms import load_norms
from geo_engine.placement import greedy_select

NORMS = load_norms()
PLANTING_TYPES = ["tree", "shrub", "lawn"]

# Buffers are polygons approximating circles (shapely quad_segs), so a
# buffered boundary is never *exactly* the nominal setback/spacing distance
# away — there's always some residual chordal error. `placement.py`'s canopy
# buffer and `buffers._BUFFER_QUAD_SEGS` both use quad_segs=32, which keeps
# that under ~0.1mm; this tolerance reflects that floor, it isn't slack for a
# real bug. Random point placement (candidates.py's dart-throwing) can land
# candidates arbitrarily close to the exact spacing/setback threshold, unlike
# the old regular grid, which never produced a "just barely under" distance —
# that's what first surfaced how tight `1e-6` actually was.
_SETBACK_TOLERANCE_M = 1e-3


def test_buildable_area_is_subset_of_territory(synthetic_scene):
    territory, utilities, zones = synthetic_scene["territory"], synthetic_scene["utilities"], synthetic_scene["zones"]
    exclusion = build_exclusion_zone(utilities, zones, "tree", NORMS)
    buildable = buildable_area(territory, exclusion, zones)
    assert buildable.within(territory.buffer(1e-6))


def test_buildable_area_excludes_existing_greenery(synthetic_scene):
    """existing_greenery must be a hard obstacle (exact footprint subtracted),
    not just a soft ml_scoring distance feature — otherwise the placement
    algorithm can recommend planting directly inside already-planted areas.
    """
    territory, utilities, zones = synthetic_scene["territory"], synthetic_scene["utilities"], synthetic_scene["zones"]
    greenery = next(z.geometry for z in zones if z.zone_type == "existing_greenery")

    exclusion = build_exclusion_zone(utilities, zones, "tree", NORMS)
    buildable = buildable_area(territory, exclusion, zones)

    assert buildable.intersection(greenery).area < 1e-6


def test_buildable_area_does_not_exclude_existing_lawn():
    """existing_lawn (Заливки.dwg's "гзн" surface-fill code -- layer_rules.py)
    is real ground cover, not a hard obstacle: a tree/shrub/lawn candidate is
    allowed to land right on top of already-grassy ground, same as it's
    allowed to land on newly-generated lawn (see CLAUDE.md's "Дерево/куст
    поверх газона" section). Regression for the split from existing_greenery
    -- before it, "гзн" layers fed the same hard-obstacle bucket as real
    existing trees/shrubs.
    """
    territory = box(0, 0, 50, 50)
    lawn = box(10, 10, 40, 40)
    zones = [Zone(geometry=territory, zone_type="territory"), Zone(geometry=lawn, zone_type="existing_lawn")]

    exclusion = build_exclusion_zone([], zones, "tree", NORMS)
    buildable = buildable_area(territory, exclusion, zones)

    assert buildable.intersection(lawn).area == pytest.approx(lawn.area)


def test_buildable_area_excludes_a_real_sidewalk_polygon():
    """sidewalk joined HARD_OBSTACLE_ZONE_TYPES (buffers.py) once real Polygon
    geometry for it existed to subtract at all -- live case, 4. Харьковская
    улица: "ДВ_ПП_ДО_ТипN_..." pavement-repair-scope layers (layer_rules.py)
    give real sidewalk-surface polygons, and a wide sidewalk's own setback
    (0.7 m from its edge) isn't enough on its own to keep a candidate off a
    2m+ wide paved path -- same reasoning as building/road/existing_greenery
    here. Historically this was a no-op (sidewalk was always a LineString,
    silently skipped by the Polygon-only filter); this is the regression for
    what happens now that it isn't always one.
    """
    territory = box(0, 0, 50, 50)
    sidewalk = box(10, 10, 15, 40)  # a real, wide paved path, not a bare edge line
    zones = [Zone(geometry=territory, zone_type="territory"), Zone(geometry=sidewalk, zone_type="sidewalk")]

    exclusion = build_exclusion_zone([], zones, "tree", NORMS)
    buildable = buildable_area(territory, exclusion, zones)

    assert buildable.intersection(sidewalk).area < 1e-6


def test_buildable_area_ignores_non_polygonal_hard_obstacles():
    """Real Мосгеотрест data reads plenty of buildings/roads as bare
    LineString (an unclosed footprint outline) or even a stray Point -- see
    CLAUDE.md's "Здания на реальном чертеже -- не полигон". Either already
    contributes zero area to a difference against a polygon, but mixing them
    into `unary_union(hard_obstacles)` used to make that a heterogeneous
    GeometryCollection -- and GEOS's overlay engine cannot always compute a
    result dimension for that as `difference()`'s second operand. Live crash
    on real data (17. Грузинская М ул, thousands of such LineStrings/Points):
    "AssertionFailedException: ... determine overlay result geometry
    dimension". Only Polygon/MultiPolygon obstacles should reach the union;
    this must not crash, and must still subtract exactly the real building.
    """
    territory = box(0, 0, 10, 10)
    real_building = box(2, 2, 4, 4)
    unclosed_building_outline = LineString([(6, 6), (8, 6), (8, 8)])
    stray_vertex = Point(5, 5)
    zones = [
        Zone(geometry=real_building, zone_type="building"),
        Zone(geometry=unclosed_building_outline, zone_type="building"),
        Zone(geometry=stray_vertex, zone_type="building"),
    ]

    buildable = buildable_area(territory, unary_union([]), zones)

    assert buildable.intersection(real_building).area < 1e-9
    assert abs(buildable.area - (territory.area - real_building.area)) < 1e-9


def test_exclusion_zone_grows_with_more_utilities(synthetic_scene):
    utilities, zones = synthetic_scene["utilities"], synthetic_scene["zones"]
    small = build_exclusion_zone(utilities[:1], [], "tree", NORMS)
    large = build_exclusion_zone(utilities, zones, "tree", NORMS)
    assert large.area >= small.area


def test_candidates_all_inside_buildable_area(synthetic_scene):
    territory, utilities, zones = synthetic_scene["territory"], synthetic_scene["utilities"], synthetic_scene["zones"]
    for planting_type in PLANTING_TYPES:
        exclusion = build_exclusion_zone(utilities, zones, planting_type, NORMS)
        buildable = buildable_area(territory, exclusion, zones)
        candidates = generate_candidates(buildable, exclusion, planting_type, NORMS, zoning_zones=zones)
        for candidate in candidates:
            assert buildable.buffer(1e-6).contains(candidate.geometry)


def test_greedy_select_respects_species_spacing(synthetic_scene):
    """No two selected same-type point plantings should end up closer than
    the species' minimum spacing requirement — that's the whole point of the
    canopy-footprint overlap check in geo_engine.placement.greedy_select.
    """
    territory, utilities, zones = synthetic_scene["territory"], synthetic_scene["utilities"], synthetic_scene["zones"]

    def score_fn(candidates):
        return [(c.clearance_m if c.clearance_m != float("inf") else 5.0, "test") for c in candidates]

    for planting_type in ["tree", "shrub"]:
        exclusion = build_exclusion_zone(utilities, zones, planting_type, NORMS)
        buildable = buildable_area(territory, exclusion, zones)
        candidates = generate_candidates(buildable, exclusion, planting_type, NORMS, zoning_zones=zones)
        items = greedy_select(candidates, score_fn, NORMS)

        min_distance = NORMS.spacing_for(planting_type).min_distance_m
        for i, a in enumerate(items):
            for b in items[i + 1 :]:
                assert a.geometry.distance(b.geometry) >= min_distance - _SETBACK_TOLERANCE_M


def test_user_overridden_spacing_actually_changes_selected_distance(synthetic_scene):
    """End-to-end check for PlantingNorms.with_spacing_override (the
    per-generation interval control): plugging an overridden norms object
    into the same real candidates/greedy_select pipeline other tests use
    here must produce trees genuinely spaced by the new interval, not the
    YAML default -- exercising the whole path, not just the norms object in
    isolation (see tests/test_geo_engine/test_norms.py for that)."""
    territory, utilities, zones = synthetic_scene["territory"], synthetic_scene["utilities"], synthetic_scene["zones"]
    overridden = NORMS.with_spacing_override("tree", 10.0)
    assert overridden.spacing_for("tree").min_distance_m != NORMS.spacing_for("tree").min_distance_m

    def score_fn(candidates):
        return [(c.clearance_m if c.clearance_m != float("inf") else 5.0, "test") for c in candidates]

    exclusion = build_exclusion_zone(utilities, zones, "tree", overridden)
    buildable = buildable_area(territory, exclusion, zones)
    candidates = generate_candidates(buildable, exclusion, "tree", overridden, zoning_zones=zones)
    items = greedy_select(candidates, score_fn, overridden)

    assert len(items) > 1  # otherwise the spacing check below is vacuous
    for i, a in enumerate(items):
        for b in items[i + 1 :]:
            assert a.geometry.distance(b.geometry) >= 10.0 - _SETBACK_TOLERANCE_M


def test_selected_items_respect_every_setback_distance(synthetic_scene):
    """Every selected item must clear *its own object type's* required
    setback from *every* utility/zone that has one — not just "some buffer
    was applied somewhere". Originally written when `lawn`'s setbacks were
    all 0.0 (a draft planting_norms.yaml value, not a bug — confirmed by
    tree/shrub passing this same check) and later caught a real sub-mm
    buffer-precision regression once lawn got small (0.3-0.5m) real setbacks
    — see `_SETBACK_TOLERANCE_M`.
    """
    territory, utilities, zones = synthetic_scene["territory"], synthetic_scene["utilities"], synthetic_scene["zones"]
    scorer = HeuristicScorer(NORMS)

    for planting_type in PLANTING_TYPES:
        exclusion = build_exclusion_zone(utilities, zones, planting_type, NORMS)
        buildable = buildable_area(territory, exclusion, zones)
        candidates = generate_candidates(buildable, exclusion, planting_type, NORMS, zoning_zones=zones)
        items = greedy_select(candidates, scorer.as_score_fn(), NORMS)

        for item in items:
            for utility in utilities:
                required = NORMS.setback_for(utility.object_type, planting_type)
                assert item.geometry.distance(utility.geometry) >= required - _SETBACK_TOLERANCE_M
            for zone in zones:
                if zone.zone_type not in NORMS.setbacks_m:
                    continue
                required = NORMS.setback_for(zone.zone_type, planting_type)
                assert item.geometry.distance(zone.geometry) >= required - _SETBACK_TOLERANCE_M


def test_territory_margin_keeps_plantings_off_the_property_line(synthetic_scene):
    """buffers.buildable_area()'s territory_margin_m erodes the territory
    boundary inward before generating candidates for that type -- without it,
    a candidate could land right at the edge of the plot (found live on real
    data by the user, see CLAUDE.md/decision_log.md). Every selected item
    must clear its own planting type's configured margin from the
    territory's own boundary line, not just from obstacles inside it.
    """
    territory, utilities, zones = synthetic_scene["territory"], synthetic_scene["utilities"], synthetic_scene["zones"]
    boundary = territory.boundary
    scorer = HeuristicScorer(NORMS)

    for planting_type in PLANTING_TYPES:
        exclusion = build_exclusion_zone(utilities, zones, planting_type, NORMS)
        margin = NORMS.territory_margin_for(planting_type)
        buildable = buildable_area(territory, exclusion, zones, territory_margin_m=margin)
        candidates = generate_candidates(buildable, exclusion, planting_type, NORMS, zoning_zones=zones)
        items = greedy_select(candidates, scorer.as_score_fn(), NORMS)

        assert items, f"expected at least one {planting_type} item to make this check non-vacuous"
        for item in items:
            assert item.geometry.distance(boundary) >= margin - _SETBACK_TOLERANCE_M


def test_generate_point_candidates_is_deterministic_for_a_given_seed(synthetic_scene):
    """generate_plan must stay a pure function of its recipe (CLAUDE.md) --
    ensure_materialized() has to reproduce a collapsed plan's exact layout
    later, not a fresh random one. Candidate generation is now randomized
    (dart-throwing, see candidates.py) instead of gridded, so this
    reproducibility guarantee is no longer automatic -- it depends entirely
    on the same seed producing the exact same raw candidates.
    """
    territory, utilities, zones = synthetic_scene["territory"], synthetic_scene["utilities"], synthetic_scene["zones"]
    exclusion = build_exclusion_zone(utilities, zones, "tree", NORMS)
    buildable = buildable_area(territory, exclusion, zones)

    first = generate_candidates(buildable, exclusion, "tree", NORMS, zoning_zones=zones, seed=12345)
    second = generate_candidates(buildable, exclusion, "tree", NORMS, zoning_zones=zones, seed=12345)

    assert len(first) == len(second) > 0
    assert [c.geometry.coords[:] for c in first] == [c.geometry.coords[:] for c in second]


def test_generate_point_candidates_differs_across_seeds(synthetic_scene):
    """Different seeds (a different plan_id/planting_type pair, see
    pipeline_service._compute_planting_rows) must give a genuinely different
    scatter -- otherwise the randomization isn't actually doing anything."""
    territory, utilities, zones = synthetic_scene["territory"], synthetic_scene["utilities"], synthetic_scene["zones"]
    exclusion = build_exclusion_zone(utilities, zones, "tree", NORMS)
    buildable = buildable_area(territory, exclusion, zones)

    a = generate_candidates(buildable, exclusion, "tree", NORMS, zoning_zones=zones, seed=1)
    b = generate_candidates(buildable, exclusion, "tree", NORMS, zoning_zones=zones, seed=2)

    assert [c.geometry.coords[:] for c in a] != [c.geometry.coords[:] for c in b]


class TestInvalidHardObstacles:
    """Невалидный полигон среди твёрдых препятствий не должен уносить улицу.

    Живой отказ на «13. Харьковский проезд»: весь прогон падал с
    `TopologyException: side location conflict`, отчёта не было вообще.
    Замер по этой улице: твёрдых препятствий 921, из них невалидных **7
    (0,76 %)**, все `Self-intersection`. Обмерные чертежи рисуют здания
    полилиниями, которые сходятся не идеально, а
    `reconstruct_closed_footprints()` собирает из них полигоны — часть колец
    выходит с самопересечением, и считать оверлей на таком входе GEOS не
    обязан.
    """

    @staticmethod
    def _bowtie():
        """Самопересекающийся контур — та же болезнь, что на реальных данных."""
        from shapely.geometry import Polygon

        bad = Polygon([(0, 0), (10, 10), (10, 0), (0, 10)])
        assert not bad.is_valid, "фикстура обязана быть невалидной"
        return bad

    def test_union_survives_a_self_intersecting_polygon(self):
        from geo_engine.buffers import valid_polygonal_union

        result = valid_polygonal_union([box(20, 20, 30, 30), self._bowtie()])

        assert not result.is_empty
        assert result.is_valid
        assert result.area > 100, "квадрат 10x10 плюс починенный контур"

    def test_repair_does_not_leak_non_areal_parts(self):
        """`make_valid` разворачивает самопересечение в GeometryCollection с
        линиями внутри, а смешанная размерность второго операнда
        `difference()` — отдельный краш, который уже чинили (17. Грузинская
        М ул). Вернуть его этой правкой было бы обидно.
        """
        from geo_engine.buffers import valid_polygonal_union

        result = valid_polygonal_union([self._bowtie()])

        assert result.geom_type in ("Polygon", "MultiPolygon"), result.geom_type

    def test_empty_input_is_not_an_error(self):
        from geo_engine.buffers import valid_polygonal_union

        assert valid_polygonal_union([]).is_empty

    def test_buildable_area_survives_an_invalid_building(self):
        norms = load_norms()
        territory = box(0, 0, 100, 100)
        zones = [
            Zone(geometry=territory, zone_type="territory"),
            Zone(geometry=self._bowtie(), zone_type="building"),
        ]
        exclusion = build_exclusion_zone([], zones, "tree", norms)

        buildable = buildable_area(territory, exclusion, zones)

        assert not buildable.is_empty
        assert buildable.area < territory.area, "препятствие должно что-то вычесть"


class TestDuplicateTerritoryContours:
    """Повторы одного контура не должны съедать участок.

    Живой отказ на «4. Харьковская улица», и он тихий — худший вид. Бандл
    ссылается на один чертёж четырежды, поэтому приходят четыре копии
    каждого контура. Дубликат по определению содержится в уже собранном
    результате на 100 %, логика дырок принимает его за внутреннее кольцо и
    вычитает только что добавленное; следующая копия добавляет обратно, и на
    чётном числе копий остаётся ноль. Итог: территория 0 м², план пустой, а
    CLI рапортует «Готово» с кодом возврата 0.
    """

    @staticmethod
    def _zones(*geoms):
        return [Zone(geometry=g, zone_type="territory") for g in geoms]

    def test_four_copies_of_one_contour_are_one_contour(self):
        from geo_engine.territory import territory_polygon

        outline = box(0, 0, 100, 100)

        result = territory_polygon(self._zones(*([outline] * 4)))

        assert result.area == pytest.approx(outline.area), "копии не должны вычитаться"

    def test_a_real_hole_still_works_alongside_duplicates(self):
        """Дедупликация не должна отменить сам механизм дырок — он нужен
        («1. Олимпийская деревня»: внешний контур минус восемь вырезов).
        """
        from geo_engine.territory import territory_polygon

        outer, hole = box(0, 0, 100, 100), box(40, 40, 60, 60)

        result = territory_polygon(self._zones(outer, outer, hole, hole))

        assert result.area == pytest.approx(outer.area - hole.area)

    def test_an_empty_result_is_an_error_not_a_silent_success(self, monkeypatch):
        """Отдельная защита, независимая от причины схлопывания.

        Конкретный случай Харьковской чинит дедупликация выше, но сам по себе
        «успех на пустой территории» — отдельный дефект: пустая территория
        даёт пустой план, а CLI рапортует «Готово» с кодом возврата 0.
        Следующая такая геометрия придёт с другой улицы, поэтому проверяется
        именно защита, а не подогнанные под ноль фигуры.
        """
        from shapely.geometry import Polygon

        from geo_engine import territory as territory_module
        from geo_engine.territory import MissingTerritoryError, territory_polygon

        monkeypatch.setattr(territory_module, "_combine_with_holes", lambda pieces: Polygon())

        with pytest.raises(MissingTerritoryError, match="не осталось площади"):
            territory_polygon(self._zones(box(0, 0, 100, 100), box(200, 200, 300, 300)))


class TestInvalidUtilityGeometry:
    """Невалидная сеть даёт неверную охранную зону — молча.

    Живой отказ на «1. Олимпийская деревня»: сирень встала в 0,76 м от
    водопровода при требуемом метре. Генератор считал место законным, а
    проверка мерила расстояние до исходной геометрии и видела нарушение —
    то есть план, который отвергает собственная проверка сервиса.

    Причина: труба пришла из чертежа самопересекающимся полигоном, а
    `buffer()` на невалидном входе не падает, он возвращает неверную фигуру.
    Проверено на той самой трубе: её буфер в 1,0 м точку НЕ накрывал, после
    `make_valid` — накрывает. Масштаб: невалидны 763 сети из 93 715 (0,81 %)
    на одной улице.
    """

    @staticmethod
    def _bowtie():
        from shapely.geometry import Polygon

        bad = Polygon([(0, 0), (10, 10), (10, 0), (0, 10)])
        assert not bad.is_valid, "фикстура обязана быть невалидной"
        return bad

    def test_repair_touches_only_what_is_broken(self):
        from geo_engine.buffers import repaired_for_buffering

        healthy = box(0, 0, 1, 1)
        out = repaired_for_buffering([healthy])

        assert out[0] is healthy, "валидное трогать незачем — это лишняя работа на сотнях тысяч объектов"

    def test_repair_makes_an_invalid_utility_valid(self):
        from geo_engine.buffers import repaired_for_buffering

        out = repaired_for_buffering([self._bowtie()])

        assert out[0].is_valid

    def test_a_line_utility_stays_a_line(self):
        """В отличие от твёрдых препятствий, площадные части здесь выделять
        нельзя: сеть — чаще всего линия, и буфер вокруг неё и есть охранная
        зона. Отбросить линейное значило бы потерять почти все сети.
        """
        from shapely.geometry import LineString

        from geo_engine.buffers import repaired_for_buffering

        line = LineString([(0, 0), (10, 0)])
        assert repaired_for_buffering([line])[0].geom_type == "LineString"

    def test_the_exclusion_covers_the_setback_around_a_broken_utility(self):
        from geo_engine.buffers import build_exclusion_zone, repaired_for_buffering
        from geo_engine.model import Utility

        norms = load_norms()
        broken = self._bowtie()
        setback = norms.setback_for("water_pipe", "shrub")
        assert setback > 0, "тест бессмыслен при нулевом отступе"

        exclusion = build_exclusion_zone(
            [Utility(geometry=broken, object_type="water_pipe")], [], "shrub", norms
        )

        # Точка на половине нормативного отступа от НАСТОЯЩЕЙ (починенной)
        # фигуры трубы обязана попасть в зону отступов.
        probe = repaired_for_buffering([broken])[0].buffer(setback / 2).exterior.coords[0]
        assert exclusion.contains(Point(probe)), "охранная зона не накрыла отступ от сломанной трубы"
