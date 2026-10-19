"""«Исправить нарушение в один клик» (п. 1 редизайна): варианты для посадки и
их применение пачкой (backend/app/services/fix_service.py).

Главное свойство — не форма ответа, а что исправление на самом деле исправляет:
предложенная точка проходит ту же проверку, что и весь план (validate_*), а не
собственную копию правил. Территория у всех сцен — квадрат 100×100 м, ограничение —
силовой кабель (дерево — не ближе 2 м, куст — не ближе 0,7 м).
"""

from __future__ import annotations

import math

import pytest
from shapely.geometry import LineString, Point, Polygon

from backend.app.db.models import Layer, Plan, PlantingItemRow, Project
from backend.app.services.edit_service import ItemsNotFoundError, validate_items, validate_plan
from backend.app.services.fix_service import apply_fixes, item_fixes
from backend.app.services.geo_io import db_to_shape, shape_to_db

TERRITORY = Polygon([(0, 0), (0, 100), (100, 100), (100, 0)])
NARROW = "Рябина обыкновенная"  # узкая крона: интервал по классу кроны 3 м, отступы без надбавок


def _scene(project_id: str, cable: LineString | None, territory: Polygon = TERRITORY, rows: list[PlantingItemRow] | None = None) -> Plan:
    """Уникальный id проекта на сцену: зоны отступов кэшируются по id (exclusion_cache)."""
    project = Project(id=project_id, name="fix")
    project.layers = [Layer(id=f"{project_id}-t", project_id=project_id, kind="zone", object_type="territory", geometry=shape_to_db(territory))]
    if cable is not None:
        project.layers.append(Layer(id=f"{project_id}-c", project_id=project_id, kind="utility", object_type="cable_line", geometry=shape_to_db(cable)))
    plan = Plan(id=f"{project_id}-plan", project_id=project_id)
    plan.project = project
    plan.items = list(rows or [])
    plan.item_count = len(plan.items)
    return plan


def _tree(item_id: str, x: float, y: float, group: str | None = None, species: str = NARROW, planting_type: str = "tree") -> PlantingItemRow:
    return PlantingItemRow(id=item_id, plan_id="p", geometry=shape_to_db(Point(x, y)), planting_type=planting_type, species=species, group_id=group)


def _by_kind(options: list[dict]) -> dict[str, dict]:
    return {option["kind"]: option for option in options}


def _violators(plan: Plan) -> set[str]:
    return {v["item_id"] for v in validate_plan(plan.project, plan)}


class TestMove:
    def test_the_proposed_spot_passes_the_same_validation_as_the_whole_plan(self):
        plan = _scene("fx-validate", LineString([(0, 50), (100, 50)]), rows=[_tree("a", 40, 49.0)])
        assert _violators(plan) == {"a"}

        move = _by_kind(item_fixes(plan, "a", None))["move"]
        outcome = apply_fixes(plan, ["a"], "move", None)

        assert outcome.skipped == []
        assert _violators(plan) == set(), "сдвиг в предложенную точку действительно снимает нарушение"
        x, y = db_to_shape(plan.items[0].geometry).coords[0]
        assert (x, y) == pytest.approx((move["to"]["x"], move["to"]["y"]), abs=1e-9), "предпросмотр = итоговая позиция"
        assert math.hypot(x - 40, y - 49.0) == pytest.approx(move["shift_m"], abs=0.01)
        assert move["clearance_m"] >= 2.0

    def test_a_tree_in_a_row_slides_along_the_row_instead_of_leaving_it(self):
        # Ряд вдоль y = 30, шаг 10 м; вертикальный кабель в 0,5 м от дерева x = 30.
        row = [_tree(f"r{i}", 10 + i * 10, 30.0, group="tree-row-0-1") for i in range(5)]
        plan = _scene("fx-row", LineString([(30.5, 0), (30.5, 60)]), rows=row)
        assert _violators(plan) == {"r2"}

        move = _by_kind(item_fixes(plan, "r2", None))["move"]

        assert move["keeps_row"] is True
        assert move["to"]["y"] == pytest.approx(30.0, abs=0.2)
        assert move["to"]["x"] < 30.5 - 2.0, "по эту сторону кабеля, не ближе отступа"
        assert move["to"]["x"] - 20 >= 3.0, "и не ближе интервала по классу кроны к соседу"

    def test_a_tree_not_in_a_row_just_takes_the_nearest_legal_spot(self):
        plan = _scene("fx-single", LineString([(0, 50), (100, 50)]), rows=[_tree("a", 40, 49.5)])

        move = _by_kind(item_fixes(plan, "a", None))["move"]

        assert move["keeps_row"] is None
        # Кабель в 0,5 м, нужно 2 м: до границы зоны 1,5 м и сетка 0,1 м даёт 1,6.
        assert move["shift_m"] == pytest.approx(1.6, abs=0.11), "ближайшая допустимая — сразу за границей зоны отступов"

    def test_the_spot_respects_the_crown_interval_to_neighbours(self):
        # Сосед в 3,5 м слева: сдвиг влево нарушил бы интервал, значит — вправо/в сторону.
        plan = _scene(
            "fx-spacing",
            LineString([(0, 50), (100, 50)]),
            rows=[_tree("a", 40, 49.0), _tree("n", 36.5, 46.0)],
        )

        move = _by_kind(item_fixes(plan, "a", None))["move"]
        apply_fixes(plan, ["a"], "move", None)

        assert math.hypot(move["to"]["x"] - 36.5, move["to"]["y"] - 46.0) >= 3.0
        assert _violators(plan) == set()

    def test_no_move_when_there_is_no_room_within_reach(self):
        # Территория — полоса в 1 м вокруг кабеля: любая точка внутри — в охранной зоне.
        strip = Polygon([(0, 49), (100, 49), (100, 51), (0, 51)])
        plan = _scene("fx-noroom", LineString([(0, 50), (100, 50)]), territory=strip, rows=[_tree("a", 50, 50.5)])

        kinds = [option["kind"] for option in item_fixes(plan, "a", None)]

        assert "move" not in kinds
        assert "delete" in kinds, "у каждого нарушения есть хотя бы один вариант"


class TestRetype:
    def test_a_shrub_is_offered_when_it_fits_there(self):
        plan = _scene("fx-shrub", LineString([(0, 50), (100, 50)]), rows=[_tree("a", 40, 49.0)])  # 1 м: дереву мало, кусту (0,7 м) хватает

        options = _by_kind(item_fixes(plan, "a", None))

        assert options["retype"]["type"] == "shrub" and options["retype"]["valid"] is True
        assert options["retype"]["required_m"] == pytest.approx(0.7)

    def test_no_shrub_when_the_shrub_would_violate_too(self):
        plan = _scene("fx-noshrub", LineString([(0, 50), (100, 50)]), rows=[_tree("a", 40, 49.8)])  # 0,2 м

        assert "retype" not in _by_kind(item_fixes(plan, "a", None))

    def test_a_shrub_is_never_offered_a_tree(self):
        plan = _scene("fx-shrub-only", LineString([(0, 50), (100, 50)]), rows=[_tree("s", 40, 49.9, planting_type="shrub", species="Спирея серая")])

        assert "retype" not in _by_kind(item_fixes(plan, "s", None))


class TestDelete:
    def test_it_names_the_gap_the_row_is_left_with(self):
        row = [_tree(f"r{i}", 10 + i * 10, 30.0, group="tree-row-0-1") for i in range(5)]
        plan = _scene("fx-gap", None, rows=row)

        assert _by_kind(item_fixes(plan, "r2", None))["delete"]["gap_m"] == pytest.approx(20.0)
        assert _by_kind(item_fixes(plan, "r0", None))["delete"]["gap_m"] is None, "с краю пропуска не остаётся"

    def test_recommended_is_marked_only_when_there_is_a_choice(self):
        strip = Polygon([(0, 49), (100, 49), (100, 51), (0, 51)])
        plan = _scene("fx-rec-one", LineString([(0, 50), (100, 50)]), territory=strip, rows=[_tree("a", 50, 50.5)])
        assert not any(option["recommended"] for option in item_fixes(plan, "a", None))

        plan = _scene("fx-rec-many", LineString([(0, 50), (100, 50)]), rows=[_tree("a", 40, 49.0)])
        options = item_fixes(plan, "a", None)
        assert [option["recommended"] for option in options] == [True] + [False] * (len(options) - 1)
        assert options[0]["kind"] == "move"


class TestApplyFixes:
    def test_every_violating_tree_gets_its_own_best_move(self):
        plan = _scene(
            "fx-bulk",
            LineString([(0, 50), (100, 50)]),
            rows=[_tree("a", 20, 49.0), _tree("b", 70, 50.6), _tree("ok", 40, 20.0)],
        )

        outcome = apply_fixes(plan, ["a", "b"], "move", None)

        assert outcome.skipped == []
        assert [f["properties"]["id"] for f in outcome.created] == ["a", "b"]
        assert [f["properties"]["id"] for f in outcome.previous] == ["a", "b"]
        assert list(outcome.previous[0]["geometry"]["coordinates"]) == [20.0, 49.0], "снимок — до правки, для отмены"
        assert _violators(plan) == set()
        assert plan.has_manual_edits is True
        assert db_to_shape(plan.items[2].geometry).equals(Point(40, 20.0)), "посадки вне пачки не трогаются"

    def test_trees_fixed_in_one_batch_do_not_crowd_each_other(self):
        # Два дерева рядом, оба нарушают отступ от кабеля; их сдвиги учитывают друг друга.
        plan = _scene("fx-bulk-pair", LineString([(0, 50), (100, 50)]), rows=[_tree("a", 40, 49.5), _tree("b", 41.5, 49.5)])

        apply_fixes(plan, ["a", "b"], "move", None)

        assert _violators(plan) == set(), "и отступ от кабеля, и интервал между ними"

    def test_items_without_the_requested_variant_are_skipped_not_forced(self):
        plan = _scene(
            "fx-skip",
            LineString([(0, 50), (100, 50)]),
            rows=[_tree("fits", 40, 49.0), _tree("tight", 60, 49.8)],
        )

        outcome = apply_fixes(plan, ["fits", "tight"], "retype", None)

        assert outcome.skipped == ["tight"]
        assert [f["properties"]["planting_type"] for f in outcome.created] == ["shrub"]
        assert {row.id: row.planting_type for row in plan.items} == {"fits": "shrub", "tight": "tree"}

    def test_delete_removes_and_recounts(self):
        plan = _scene("fx-del", LineString([(0, 50), (100, 50)]), rows=[_tree("a", 40, 49.0), _tree("b", 40, 20.0)])

        outcome = apply_fixes(plan, ["a"], "delete", None)

        assert outcome.created == [] and len(outcome.previous) == 1
        assert [row.id for row in plan.items] == ["b"]
        assert plan.item_count == 1

    def test_an_unknown_id_changes_nothing(self):
        plan = _scene("fx-unknown", LineString([(0, 50), (100, 50)]), rows=[_tree("a", 40, 49.0)])

        with pytest.raises(ItemsNotFoundError):
            apply_fixes(plan, ["a", "gone"], "move", None)

        assert db_to_shape(plan.items[0].geometry).equals(Point(40, 49.0))

    def test_a_fixed_item_revalidates_clean_through_the_incremental_path(self):
        plan = _scene("fx-incremental", LineString([(0, 50), (100, 50)]), rows=[_tree("a", 40, 49.0)])

        apply_fixes(plan, ["a"], "move", None)

        assert validate_items(plan.project, plan, ["a"]) == []


class TestNearestAllowed:
    """Alt при перетаскивании: ближайшая допустимая точка вокруг курсора."""

    def test_a_cursor_inside_the_zone_is_pulled_to_its_edge(self):
        from backend.app.services.fix_service import nearest_allowed

        plan = _scene("fx-alt", LineString([(0, 50), (100, 50)]), rows=[_tree("a", 40, 20.0)])

        found = nearest_allowed(plan, "a", 40.0, 50.5, None)  # курсор в 0,5 м от кабеля: нужно 2 м

        assert found is not None
        assert found["to"]["y"] == pytest.approx(52.1, abs=0.15) or found["to"]["y"] == pytest.approx(47.9, abs=0.15)
        assert 1.4 <= found["shift_m"] <= 1.8

    def test_the_answer_passes_the_real_check_when_applied(self):
        from backend.app.services.fix_service import nearest_allowed

        plan = _scene("fx-alt-check", LineString([(0, 50), (100, 50)]), rows=[_tree("a", 40, 20.0)])
        found = nearest_allowed(plan, "a", 41.0, 50.2, None)
        assert found is not None
        plan.items[0].geometry = shape_to_db(Point(found["to"]["x"], found["to"]["y"]))

        assert _violators(plan) == set()

    def test_no_place_nearby_gives_none(self):
        from backend.app.services.fix_service import nearest_allowed

        strip = Polygon([(0, 49), (100, 49), (100, 51), (0, 51)])
        plan = _scene("fx-alt-none", LineString([(0, 50), (100, 50)]), territory=strip, rows=[_tree("a", 50, 50.5)])

        assert nearest_allowed(plan, "a", 50.0, 50.2, None) is None


class TestNearbyConstraints:
    def test_zones_carry_the_norm_for_this_planting_and_the_object(self):
        from backend.app.services.constraints_service import nearby_constraints

        plan = _scene("nc-basic", LineString([(0, 50), (100, 50)]), rows=[_tree("a", 40, 49.0)])

        result = nearby_constraints(plan, 40.0, 49.0, 15.0, "tree", "a", None)

        cables = [f for f in result["features"] if f["object_type"] == "cable_line"]
        assert len(cables) == 1 and cables[0]["kind"] == "constraint"
        assert cables[0]["required_m"] == pytest.approx(2.0)
        assert cables[0]["zone"]["type"] in ("Polygon", "MultiPolygon")

    def test_the_norm_depends_on_the_planting_type(self):
        from backend.app.services.constraints_service import nearby_constraints

        plan = _scene("nc-shrub", LineString([(0, 50), (100, 50)]), rows=[_tree("a", 40, 49.0)])

        tree = nearby_constraints(plan, 40.0, 49.0, 15.0, "tree", None, None)["features"]
        shrub = nearby_constraints(plan, 40.0, 49.0, 15.0, "shrub", None, None)["features"]

        assert tree[0]["required_m"] == pytest.approx(2.0)
        assert shrub[0]["required_m"] == pytest.approx(0.7), "кусту — мягче: СП 42.13330.2016 табл. 9.1"

    def test_a_neighbouring_tree_brings_its_crown_interval(self):
        from backend.app.services.constraints_service import nearby_constraints

        plan = _scene("nc-neighbour", None, rows=[_tree("a", 40, 40.0), _tree("n", 43, 40.0)])

        found = nearby_constraints(plan, 40.0, 40.0, 15.0, "tree", "a", None)["features"]

        assert [f["id"] for f in found if f["kind"] == "neighbour"] == ["n"], "саму тянутую посадку за соседа не считаем"
        assert next(f for f in found if f["kind"] == "neighbour")["required_m"] == pytest.approx(3.0)

    def test_objects_beyond_the_radius_are_left_out(self):
        from backend.app.services.constraints_service import nearby_constraints

        plan = _scene("nc-far", LineString([(0, 90), (100, 90)]), rows=[_tree("a", 40, 10.0)])

        assert nearby_constraints(plan, 40.0, 10.0, 15.0, "tree", "a", None)["features"] == []

    def test_long_lines_are_cut_to_the_window_around_the_point(self):
        from backend.app.services.constraints_service import nearby_constraints

        plan = _scene("nc-clip", LineString([(0, 50), (100, 50)]), rows=[_tree("a", 40, 49.0)])

        feature = nearby_constraints(plan, 40.0, 49.0, 15.0, "tree", "a", None)["features"][0]
        xs = [c[0] for c in feature["geometry"]["coordinates"]]

        assert min(xs) >= 40 - 15 - 2.0 - 1e-6 and max(xs) <= 40 + 15 + 2.0 + 1e-6, "кабель вдоль всего участка в ответ целиком не идёт"
