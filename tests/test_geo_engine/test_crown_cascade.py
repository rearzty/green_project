"""Каскад крон: в каждом месте — самая крупная крона, которой там хватает
места по нормам (planner._tree_cascade)."""

from __future__ import annotations

from shapely.geometry import LineString, box

from geo_engine.compliance import explain_items
from geo_engine.model import Utility, Zone
from geo_engine.norms import load_norms
from geo_engine.planner import plan_items
from geo_engine.species import load_catalogue


def _scene():
    # Просторный двор слева и тесная полоса справа: вдоль полосы с двух
    # сторон кабели, так что широкой кроне (отступ увеличен по прим. 1 к
    # табл. 743-ПП) там места нет, а узкой — есть.
    territory = box(0, 0, 200, 60)
    zones = [
        Zone(geometry=territory, zone_type="territory"),
        Zone(geometry=LineString([(0, 0), (200, 0)]), zone_type="road"),
    ]
    utilities = [
        Utility(geometry=LineString([(110, 0), (110, 60)]), object_type="cable_line"),
        Utility(geometry=LineString([(116, 0), (116, 60)]), object_type="cable_line"),
    ]
    return territory, zones, utilities


def _plan(key="k"):
    territory, zones, utilities = _scene()
    return plan_items(
        key, utilities, zones, territory, ["tree"], lambda c: [(1.0, "т") for _ in c], load_norms(),
        catalogue=load_catalogue(), density_per_ha={"tree": 0},
    )


def test_wide_crowns_go_where_they_fit_and_smaller_ones_fill_the_rest():
    catalogue = load_catalogue()
    crowns = {catalogue.get(i.species).crown for i in _plan()}

    assert "wide" in crowns
    assert len(crowns) >= 2, "в тесных местах должна появиться крона поменьше"


def test_cascade_plan_passes_its_own_compliance_check():
    territory, zones, utilities = _scene()
    items = _plan()

    records = explain_items(items, utilities, zones, load_norms(), load_catalogue())

    assert items and all(r.compliant for r in records)


def test_cascade_does_not_depend_on_the_plan_key():
    assert len(_plan("a")) == len(_plan("b")) == len(_plan("c"))
