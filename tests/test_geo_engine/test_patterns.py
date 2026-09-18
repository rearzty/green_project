"""Рядовая посадка вдоль линейного ориентира.

До этого генератор умел ровно одно — разбрасывать точки по допустимой площади.
Результат нормативам не противоречит, но проектом озеленения не является:
блу-нойз, который просто нигде не нарушает отступы. Сами акты описывают типы
посадки — однорядную, двухрядную, групповую (743-ПП табл. 3.6.2; МГСН 1.02-02
п. 4.2.9.2).

Замер на эталонной улице после включения схемы (липа, интервал 8 м по классу
кроны): у рядовой посадки медиана шага между соседями 8.00 м и 62% попаданий
в ±0.5 м от норматива, у свободной группы — 11.73 м и 16%.
"""

import statistics

import pytest
from shapely.geometry import LineString, Point, Polygon

from geo_engine.model import Zone
from geo_engine.norms import load_norms
from geo_engine.patterns import (
    MIN_GUIDE_LENGTH_M,
    collect_row_guides,
    points_along,
    row_candidates,
    territory_guide,
)
from geo_engine.species import load_catalogue


@pytest.fixture
def norms():
    return load_norms()


@pytest.fixture
def catalogue():
    return load_catalogue()


class TestPointsAlong:
    def test_points_are_spaced_at_the_pitch(self):
        line = LineString([(0, 0), (100, 0)])

        points = points_along(line, 10.0)

        gaps = [points[i].distance(points[i + 1]) for i in range(len(points) - 1)]
        assert all(gap == pytest.approx(10.0) for gap in gaps)

    def test_the_row_is_centred_on_the_line(self):
        """Отрезок редко делится на шаг нацело. При отсчёте от начала весь
        остаток копится на дальнем конце и ряд выглядит оборванным — здесь
        остаток делится поровну между концами.
        """
        line = LineString([(0, 0), (95, 0)])

        points = points_along(line, 10.0)

        head = points[0].distance(Point(0, 0))
        tail = points[-1].distance(Point(95, 0))
        assert head == pytest.approx(tail)

    def test_a_line_shorter_than_half_a_pitch_gets_nothing(self):
        assert points_along(LineString([(0, 0), (2, 0)]), 10.0) == []

    def test_a_line_around_one_pitch_gets_a_single_centred_point(self):
        line = LineString([(0, 0), (7, 0)])

        points = points_along(line, 10.0)

        assert len(points) == 1
        assert points[0].x == pytest.approx(3.5)


class TestGuides:
    def test_fragmented_kerb_is_merged_before_use(self):
        """Регрессия на реальный дефект: бортовой камень приезжает из чертежа
        тысячами отдельных отрезков (на эталонной улице — 6510). Первая версия
        строила «ряд» вдоль каждого огрызка — буфер вокруг двухметрового
        отрезка почти круглый, вдоль него помещается одна точка, и получалась
        та же россыпь, только дороже.
        """
        pieces = [
            Zone(geometry=LineString([(x, 0), (x + 1.0, 0)]), zone_type="road")
            for x in range(0, 60)
        ]

        guides = collect_row_guides(pieces, "tree", load_norms())

        assert len(guides) == 1, "обрывки одного бордюра должны стать одним ориентиром"
        assert guides[0].geometry.length >= 55

    def test_short_leftovers_are_dropped(self):
        """Аллеи вдоль трёхметрового бордюрного огрызка не бывает."""
        stub = [Zone(geometry=LineString([(0, 0), (5, 0)]), zone_type="road")]

        assert collect_row_guides(stub, "tree", load_norms()) == []

    def test_only_streetlike_objects_guide_a_row(self, norms):
        """Ряд вдоль газопровода формально допустим — отступ соблюдён, — но это
        не аллея, а линия вдоль трубы. Ориентиром берётся то, вдоль чего аллею
        действительно сажают.
        """
        long_line = LineString([(0, 0), (200, 0)])
        zones = [
            Zone(geometry=long_line, zone_type="road"),
            Zone(geometry=long_line, zone_type="building"),
            Zone(geometry=long_line, zone_type="existing_greenery"),
        ]

        guides = collect_row_guides(zones, "tree", norms)

        assert {g.object_type for g in guides} == {"road"}

    def test_the_guide_carries_the_species_aware_setback(self, norms, catalogue):
        """Отступ берётся через resolve_setback, а не из таблицы напрямую: он
        зависит от породы, и ряд по табличному значению оказался бы нарушением
        для крупнокронной — ровно та рассогласованность генератора и проверки,
        которая уже случалась на буферах.
        """
        zones = [Zone(geometry=LineString([(0, 0), (200, 0)]), zone_type="road")]
        linden = catalogue.get("Липа мелколистная")  # крона 8 м, отступы масштабируются

        plain = collect_row_guides(zones, "tree", norms)
        with_species = collect_row_guides(
            zones, "tree", norms, linden, catalogue.crown_reference_diameter_m
        )

        assert with_species[0].setback_m > plain[0].setback_m

    def test_territory_boundary_is_a_guide_with_its_own_margin(self, norms):
        territory = Polygon([(0, 0), (200, 0), (200, 150), (0, 150)])

        guide = territory_guide(territory, "tree", norms)

        assert guide is not None
        assert guide.setback_m == norms.territory_margin_for("tree")


class TestRowCandidates:
    def _setup(self, norms):
        territory = Polygon([(0, 0), (200, 0), (200, 120), (0, 120)])
        road = LineString([(0, 0), (200, 0)])
        setback = norms.setback_for("road", "tree")
        exclusion = road.buffer(setback)
        buildable = territory.buffer(-norms.territory_margin_for("tree")).difference(exclusion)
        return territory, road, setback, exclusion, buildable

    def test_the_row_sits_at_the_binding_constraint_not_at_the_guide_setback(self, norms):
        """Ряд прижат к улице ровно настолько, насколько позволяет норматив, —
        но связывает не обязательно сама улица.

        Здесь отступ от проезжей части 2 м, а отступ от границы участка 5 м,
        и край допустимой площади проходит по второму. Первая версия искала
        ряд на кромке буфера ориентира, то есть в 2 м от улицы — внутри
        запрещённой полосы, — и не находила ни одной точки.
        """
        _, road, setback, exclusion, buildable = self._setup(norms)
        binding = max(setback, norms.territory_margin_for("tree"))
        guides = collect_row_guides([Zone(geometry=road, zone_type="road")], "tree", norms)

        candidates = row_candidates(guides, buildable, exclusion, "tree", pitch_m=8.0)

        assert candidates
        distances = [c.geometry.distance(road) for c in candidates]
        assert min(distances) >= setback
        assert statistics.median(distances) == pytest.approx(binding, abs=0.5)
        assert binding > setback, "фикстура должна проверять именно этот случай"

    def test_candidates_are_evenly_spaced(self, norms):
        _, road, _, exclusion, buildable = self._setup(norms)
        guides = collect_row_guides([Zone(geometry=road, zone_type="road")], "tree", norms)

        candidates = row_candidates(guides, buildable, exclusion, "tree", pitch_m=8.0)
        xs = sorted(c.geometry.x for c in candidates if c.geometry.y < 10)

        gaps = [b - a for a, b in zip(xs, xs[1:])]
        assert gaps, "вдоль улицы должен получиться настоящий ряд, а не одна точка"
        # Шаг НЕ обязан равняться номиналу ровно, но обязан быть не меньше:
        # эквидистанта замкнута, и остаток от деления её длины на шаг
        # раскладывается поровну на все промежутки. Оставить остаток целиком на
        # стыке было бы хуже — там первая и последняя точки оказались бы ближе
        # шага друг к другу, то есть с нарушением интервала между стволами.
        assert min(gaps) >= 8.0 - 1e-6, "шаг ряда не может быть меньше норматива"
        assert statistics.median(gaps) == pytest.approx(8.0, rel=0.05)

    def test_nothing_lands_outside_the_buildable_area(self, norms):
        _, road, _, exclusion, buildable = self._setup(norms)
        guides = collect_row_guides([Zone(geometry=road, zone_type="road")], "tree", norms)

        candidates = row_candidates(guides, buildable, exclusion, "tree", pitch_m=8.0)

        assert all(buildable.contains(c.geometry) for c in candidates)

    def test_an_empty_buildable_area_yields_nothing(self, norms):
        _, road, _, exclusion, _ = self._setup(norms)
        guides = collect_row_guides([Zone(geometry=road, zone_type="road")], "tree", norms)

        assert row_candidates(guides, Polygon(), exclusion, "tree", pitch_m=8.0) == []


class TestPlannerPatterns:
    def _scene(self):
        territory = Polygon([(0, 0), (200, 0), (200, 120), (0, 120)])
        zones = [
            Zone(geometry=territory, zone_type="territory"),
            Zone(geometry=LineString([(0, 0), (200, 0)]), zone_type="road"),
        ]
        return territory, zones

    def _score(self, candidates):
        return [(1.0, "тест") for _ in candidates]

    def test_auto_produces_rows_and_marks_them(self, norms, catalogue):
        from geo_engine.planner import ROW_RATIONALE_PREFIX, plan_items

        territory, zones = self._scene()

        # density_per_ha={"tree": 0}: this test checks that BOTH phases
        # (row + scatter) occur, which DEFAULT_DENSITY_PER_HA can suppress
        # on a scene this small/dense (the row alone can already exceed the
        # 25/ha default, leaving nothing for the density cap to keep from
        # scatter) -- unrelated to what this test verifies, see planner.py.
        items = plan_items(
            "rows", [], zones, territory, ["tree"], self._score, norms,
            species_overrides={"tree": "Липа мелколистная"}, pattern="auto", catalogue=catalogue,
            density_per_ha={"tree": 0},
        )
        in_rows = [i for i in items if i.rationale.startswith(ROW_RATIONALE_PREFIX)]

        assert in_rows, "вдоль проезда должна появиться рядовая посадка"
        assert len(in_rows) < len(items), "остальная площадь по-прежнему заполняется россыпью"

    def test_scatter_reproduces_the_previous_behaviour(self, norms, catalogue):
        from geo_engine.planner import ROW_RATIONALE_PREFIX, plan_items

        territory, zones = self._scene()

        items = plan_items(
            "rows", [], zones, territory, ["tree"], self._score, norms,
            species_overrides={"tree": "Липа мелколистная"}, pattern="scatter", catalogue=catalogue,
        )

        assert items
        assert not any(i.rationale.startswith(ROW_RATIONALE_PREFIX) for i in items)

    def test_scatter_does_not_land_on_top_of_the_row(self, norms, catalogue):
        """У каждого прохода greedy_select свой пространственный индекс, общей
        памяти между ними нет — без вычитания крон уже поставленного ряда
        россыпь насыпала бы точки прямо на аллею.
        """
        from geo_engine.planner import plan_items

        territory, zones = self._scene()

        items = plan_items(
            "rows", [], zones, territory, ["tree"], self._score, norms,
            species_overrides={"tree": "Липа мелколистная"}, pattern="auto", catalogue=catalogue,
        )
        radius = norms.spacing_for("tree").canopy_radius_m

        for index, item in enumerate(items):
            for other in items[index + 1:]:
                assert item.geometry.distance(other.geometry) >= radius, "кроны наложились"

    def test_rows_respect_the_same_setbacks_as_everything_else(self, norms, catalogue):
        """Ряд прижат к улице ровно на норматив — значит проверка соответствия
        обязана его принять. Если нет, генератор снова ставит то, что сам же
        считает нарушением.
        """
        from geo_engine.compliance import explain_items
        from geo_engine.planner import plan_items

        territory, zones = self._scene()

        items = plan_items(
            "rows", [], zones, territory, ["tree"], self._score, norms,
            species_overrides={"tree": "Липа мелколистная"}, pattern="auto", catalogue=catalogue,
        )
        records = explain_items(items, [], zones, norms, catalogue)

        assert [r for r in records if not r.compliant] == []

    def test_scatter_keeps_the_crown_interval_away_from_the_row(self, norms, catalogue):
        """Ряд и россыпь — два независимых прохода отбора, и между ними никто
        не держал интервал.

        Ловится только на РАЗНЫХ породах у ряда и у россыпи: `greedy_select`
        внутри одной фазы интервал соблюдает, а из площади под россыпь
        вычитался радиус кроны рядовой породы — то есть половина её же
        интервала, да ещё и чужого. Живьём на эталонной улице это дало берёзу
        (интервал 5 м) и дуб (8 м) в 2,60 м друг от друга, и отчёт показывал
        полное соответствие: проверка мерила только отступы от сетей и зон.
        """
        from geo_engine.planner import ROW_RATIONALE_PREFIX, plan_items

        territory, zones = self._scene()

        # density_per_ha={"tree": 0}: needs both phases present with
        # different species to catch the defect -- DEFAULT_DENSITY_PER_HA
        # would otherwise cap the plan down to row-only on this scene (see
        # test_auto_produces_rows_and_marks_them's comment above).
        items = plan_items(
            "mixed", [], zones, territory, ["tree"], self._score, norms,
            pattern="auto", catalogue=catalogue,
            density_per_ha={"tree": 0},
        )
        rows = [i for i in items if i.rationale.startswith(ROW_RATIONALE_PREFIX)]
        loose = [i for i in items if not i.rationale.startswith(ROW_RATIONALE_PREFIX)]
        assert rows and loose, "сцена должна давать обе фазы, иначе тест ничего не проверяет"
        assert {i.species for i in rows} != {i.species for i in loose}, (
            "породы фаз должны различаться — на одинаковых дефект не проявляется"
        )

        def interval(name):
            species = catalogue.get(name)
            return catalogue.spacing_for_crown(species.crown)

        for row_item in rows:
            for loose_item in loose:
                required = max(interval(row_item.species), interval(loose_item.species))
                assert row_item.geometry.distance(loose_item.geometry) >= required - 1e-3, (
                    f"{row_item.species} и {loose_item.species} ближе {required} м"
                )
