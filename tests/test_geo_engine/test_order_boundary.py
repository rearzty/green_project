"""Граница заказа на съёмку как запасная граница участка — только с согласия
пользователя и только когда границы работ в поставке нет вовсе
(geo_engine.territory.territory_source, «7. Нижние Поля ул»)."""

from __future__ import annotations

import asyncio
import json

import ezdxf
import pytest
from shapely.geometry import LineString, box

from geo_engine.io.dxf_reader import resolve_layer
from geo_engine.io.layer_rules import classify_layer
from geo_engine.model import Zone
from geo_engine.territory import (
    MissingTerritoryError,
    OrderBoundaryOnlyError,
    territory_polygon,
    territory_source,
)
from scripts.plan_dxf import main


def _zones(with_work_area: bool) -> list[Zone]:
    zones = [Zone(geometry=box(0, 0, 300, 200), zone_type="order_boundary")]
    if with_work_area:
        zones.append(Zone(geometry=box(50, 50, 150, 120), zone_type="territory"))
    # Обрывок на слое границы работ, как на Нижних Полях: не площадной.
    zones.append(Zone(geometry=LineString([(0, 0), (9.4, 0)]), zone_type="territory"))
    return zones


def test_the_order_boundary_layer_is_recognised_in_any_case():
    assert classify_layer("Граница Заказа") == ("zone", "order_boundary")
    assert classify_layer("Граница заказа") == ("zone", "order_boundary")
    assert resolve_layer("Граница работ", {}) == ("zone", "territory")


def test_the_work_area_wins_whenever_there_is_one():
    zones = _zones(with_work_area=True)

    assert territory_source(zones) == "territory"
    assert territory_polygon(zones, allow_order_boundary=True).area == pytest.approx(100 * 70)


def test_without_consent_the_order_boundary_is_offered_not_used():
    zones = _zones(with_work_area=False)

    assert territory_source(zones) == "order_boundary"
    with pytest.raises(OrderBoundaryOnlyError, match="генерировать по границе заказа"):
        territory_polygon(zones)


def test_with_consent_the_order_boundary_becomes_the_site():
    assert territory_polygon(_zones(with_work_area=False), allow_order_boundary=True).area == pytest.approx(300 * 200)


def test_nothing_at_all_is_still_a_plain_error():
    zones = [Zone(geometry=LineString([(0, 0), (9.4, 0)]), zone_type="territory")]

    assert territory_source(zones) is None
    with pytest.raises(MissingTerritoryError) as excinfo:
        territory_polygon(zones, allow_order_boundary=True)
    assert not isinstance(excinfo.value, OrderBoundaryOnlyError)


class _Stdin:
    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _drawing_with_only_the_order_boundary(tmp_path):
    doc = ezdxf.new(setup=True)
    for layer in ("Граница Заказа", "Газопровод"):
        doc.layers.add(name=layer)
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (120, 0), (120, 90), (0, 90)], close=True, dxfattribs={"layer": "Граница Заказа"})
    msp.add_lwpolyline([(0, 45), (120, 45)], dxfattribs={"layer": "Газопровод"})
    path = tmp_path / "site.dxf"
    doc.saveas(str(path))
    return path


def test_cli_without_a_terminal_stops_and_names_the_flag(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.stdin", _Stdin(tty=False))
    source = _drawing_with_only_the_order_boundary(tmp_path)

    with pytest.raises(SystemExit) as excinfo:
        main(["--input", str(source), "--output", str(tmp_path / "out.dxf"), "--types", "tree"])

    assert "--use-order-boundary" in str(excinfo.value)


def test_cli_asks_in_a_terminal_and_proceeds_on_yes(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.stdin", _Stdin(tty=True))
    monkeypatch.setattr("builtins.input", lambda prompt="": "да")
    source = _drawing_with_only_the_order_boundary(tmp_path)
    out = tmp_path / "out.dxf"

    exit_code = main(["--input", str(source), "--output", str(out), "--types", "tree"])

    assert exit_code == 0
    report = json.loads(out.with_suffix(".report.json").read_text(encoding="utf-8"))
    assert report["territory_source"] == "order_boundary"
    assert "предварительный" in report["territory_note"]


def test_cli_flag_skips_the_question(tmp_path, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("не должен спрашивать"))
    source = _drawing_with_only_the_order_boundary(tmp_path)
    out = tmp_path / "out.dxf"

    exit_code = main(["--input", str(source), "--output", str(out), "--types", "tree", "--use-order-boundary"])

    assert exit_code == 0


def test_web_generation_needs_the_projects_consent():
    from backend.app.db.models import Layer, Project
    from backend.app.services.pipeline_service import generate_plan

    project = Project(name="t", territory_source="order_boundary")
    project.layers = [
        Layer(project_id=project.id, kind="zone", object_type="order_boundary", geometry=box(0, 0, 120, 90)),
        Layer(project_id=project.id, kind="utility", object_type="gas_pipe", geometry=LineString([(0, 45), (120, 45)])),
    ]

    with pytest.raises(OrderBoundaryOnlyError):
        asyncio.run(generate_plan(project, ["tree"], "heuristic"))

    project.use_order_boundary = True
    plan = asyncio.run(generate_plan(project, ["tree"], "heuristic"))
    assert plan.items
