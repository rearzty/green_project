"""«Ожидается» на вкладке «План» (п. 8 редизайна): диапазон числа посадок до
генерации. Тесты проверяют не точность модели (её константы подобраны по одной
улице, см. estimate_service), а что масштабирование ведёт себя как задумано:
от открытого плана — вокруг его чисел, по площади — когда плана нет, и не
выдумывает ответ там, где оценить не из чего."""

from __future__ import annotations

import pytest
from shapely.geometry import LineString, Point, box

from backend.app.db.models import Layer, Plan, PlantingItemRow, Project
from backend.app.schemas.plan import GenerateRequest
from backend.app.services.estimate_service import estimate
from backend.app.services.geo_io import shape_to_db


def _project(project_id: str, with_territory: bool = True) -> Project:
    project = Project(id=project_id, name="est")
    project.layers = []
    if with_territory:
        project.layers.append(Layer(project_id=project_id, kind="zone", object_type="territory", geometry=shape_to_db(box(0, 0, 200, 100))))
        project.layers.append(Layer(project_id=project_id, kind="utility", object_type="gas_pipe", geometry=shape_to_db(LineString([(0, 50), (200, 50)]))))
    return project


def _plan_with(project: Project, trees=100, shrubs=1000, lawns=(box(0, 0, 10, 10), box(0, 20, 20, 30)), **recipe) -> Plan:
    plan = Plan(id=f"{project.id}-plan", project_id=project.id, is_current=True, project=project, **recipe)
    rows = [PlantingItemRow(plan_id=plan.id, geometry=shape_to_db(Point(i, 1)), planting_type="tree") for i in range(trees)]
    rows += [PlantingItemRow(plan_id=plan.id, geometry=shape_to_db(Point(i, 2)), planting_type="shrub") for i in range(shrubs)]
    rows += [PlantingItemRow(plan_id=plan.id, geometry=shape_to_db(polygon), planting_type="lawn") for polygon in lawns]
    plan.items = rows
    project.plans = [plan]
    return plan


def _by_type(result: dict) -> dict[str, dict]:
    return {e["planting_type"]: e for e in result["estimates"]}


def test_the_same_recipe_brackets_the_open_plans_own_numbers():
    project = _project("est-same")
    _plan_with(project)

    found = _by_type(estimate(project, GenerateRequest()))

    assert found["tree"]["basis"] == "plan" and found["tree"]["low"] <= 100 <= found["tree"]["high"]
    assert found["shrub"]["low"] <= 1000 <= found["shrub"]["high"]
    assert found["lawn"]["unit"] == "m2" and found["lawn"]["low"] <= 300 <= found["lawn"]["high"], "газон — площадь, не штуки"


def test_a_wider_tree_interval_means_fewer_trees():
    project = _project("est-wider")
    _plan_with(project)

    narrow = _by_type(estimate(project, GenerateRequest(tree_spacing_m=6.0)))["tree"]
    wide = _by_type(estimate(project, GenerateRequest(tree_spacing_m=11.0)))["tree"]

    assert wide["high"] < narrow["low"] or wide["low"] < narrow["low"]
    assert wide["high"] < 100 and narrow["high"] < 130


def test_an_interval_below_the_crown_norm_changes_nothing_much():
    project = _project("est-floor")
    _plan_with(project)

    tight = _by_type(estimate(project, GenerateRequest(tree_spacing_m=2.0)))["tree"]

    assert tight["low"] <= 100 <= tight["high"], "нижняя граница задаётся нормой класса кроны, а не желанием пользователя"


def test_hedge_count_is_inverse_to_the_pitch():
    project = _project("est-hedge")
    _plan_with(project)

    found = _by_type(estimate(project, GenerateRequest(shrub_spacing_m=1.5)))["shrub"]  # шаг 0,5 → 1,5: втрое реже

    assert found["low"] <= 1000 / 3 <= found["high"]


def test_two_rows_do_not_double_the_count_on_a_tight_street():
    project = _project("est-rows")
    _plan_with(project)

    found = _by_type(estimate(project, GenerateRequest(tree_rows=2)))["tree"]

    assert found["high"] < 100


def test_the_plans_own_recipe_is_the_baseline_not_the_defaults():
    project = _project("est-recipe")
    _plan_with(project, tree_spacing_m=8.0)

    same = _by_type(estimate(project, GenerateRequest(tree_spacing_m=8.0)))["tree"]
    default = _by_type(estimate(project, GenerateRequest()))["tree"]

    assert same["low"] <= 100 <= same["high"]
    assert default["low"] > 100, "интервал «по умолчанию» плотнее, чем 8 м, из которого построен план"


def test_without_a_plan_it_falls_back_to_the_area_and_says_so():
    project = _project("est-area")

    found = _by_type(estimate(project, GenerateRequest()))

    assert {e["basis"] for e in found.values()} == {"area"}
    for entry in found.values():
        assert 0 < entry["low"] < entry["high"]
    assert (found["tree"]["high"] - found["tree"]["low"]) / found["tree"]["high"] > 0.5, "по площади диапазон широкий: форма участка неизвестна"


def test_without_a_territory_there_is_nothing_to_estimate():
    project = _project("est-none", with_territory=False)

    assert estimate(project, GenerateRequest())["estimates"] == []


def test_only_the_requested_types_are_estimated():
    project = _project("est-types")
    _plan_with(project)

    found = _by_type(estimate(project, GenerateRequest(planting_types=["tree"])))

    assert list(found) == ["tree"]


def test_the_route_answers(monkeypatch):
    from fastapi.testclient import TestClient

    import backend.app.db.session as store
    from backend.app.main import app

    project = _project("est-route")
    _plan_with(project)
    store.save_project(project)

    body = TestClient(app).post(f"/api/projects/{project.id}/estimate", json={"tree_spacing_m": 8.0}).json()

    assert {e["planting_type"] for e in body["estimates"]} == {"tree", "shrub", "lawn"}
    assert all(e["low"] <= e["high"] for e in body["estimates"])
