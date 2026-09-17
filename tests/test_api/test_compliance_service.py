"""compliance_service.py: the ORM-row <-> geo_engine.compliance bridge behind
the /compliance/items and /compliance-report.* routes.

Exercised against transient (un-persisted) ORM objects, same style as
test_edit_service.py -- these functions only ever read plan.items/
project.layers as plain Python attributes, never touch a session themselves,
so a real PostGIS connection isn't needed to test them.
"""

from __future__ import annotations

import csv

from shapely.geometry import LineString, Point, Polygon

from backend.app.db.models import Layer, Plan, PlantingItemRow, Project
from backend.app.services.compliance_service import explain_items_by_id, full_report, full_trace_csv
from backend.app.services.geo_io import shape_to_db
from geo_engine.norms import load_norms

TERRITORY = Polygon([(0, 0), (0, 100), (100, 100), (100, 0)])
GAS_PIPE = LineString([(0, 50), (100, 50)])


def _plan_with_gas_pipe_and_two_trees() -> Plan:
    project = Project(id="project-1", name="test")
    project.layers = [
        Layer(id="layer-territory", project_id="project-1", kind="zone", object_type="territory", geometry=shape_to_db(TERRITORY)),
        Layer(id="layer-gas", project_id="project-1", kind="utility", object_type="gas_pipe", geometry=shape_to_db(GAS_PIPE)),
    ]
    plan = Plan(id="plan-1", project_id="project-1", tree_spacing_m=None, shrub_spacing_m=None)
    plan.project = project
    plan.items = [
        PlantingItemRow(
            id="tree-far",
            plan_id="plan-1",
            geometry=shape_to_db(Point(10, 10)),
            planting_type="tree",
            species="Клён остролистный",
            score=0.8,
            rationale="",
        ),
        PlantingItemRow(
            id="tree-close",
            plan_id="plan-1",
            geometry=shape_to_db(Point(10, 49)),
            planting_type="tree",
            species="Липа мелколистная",
            score=0.8,
            rationale="",
        ),
    ]
    return plan


class TestExplainItemsById:
    def test_restricts_to_the_requested_ids(self):
        plan = _plan_with_gas_pipe_and_two_trees()
        norms = load_norms()

        pairs = explain_items_by_id(plan.project, plan, ["tree-far"], norms)

        assert [item_id for item_id, _ in pairs] == ["tree-far"]

    def test_a_planting_far_from_the_gas_pipe_is_compliant(self):
        plan = _plan_with_gas_pipe_and_two_trees()
        norms = load_norms()

        [(_, record)] = explain_items_by_id(plan.project, plan, ["tree-far"], norms)

        assert record.compliant
        assert "СП 42.13330.2016" in record.summary

    def test_a_planting_too_close_to_the_gas_pipe_is_a_violation(self):
        plan = _plan_with_gas_pipe_and_two_trees()
        norms = load_norms()

        [(_, record)] = explain_items_by_id(plan.project, plan, ["tree-close"], norms)

        assert not record.compliant
        gas_check = next(c for c in record.checks if c.object_type == "gas_pipe")
        assert not gas_check.satisfied
        assert gas_check.citation

    def test_empty_id_list_returns_nothing(self):
        plan = _plan_with_gas_pipe_and_two_trees()
        norms = load_norms()

        assert explain_items_by_id(plan.project, plan, [], norms) == []

    def test_unknown_id_is_silently_ignored_not_an_error(self):
        plan = _plan_with_gas_pipe_and_two_trees()
        norms = load_norms()

        assert explain_items_by_id(plan.project, plan, ["does-not-exist"], norms) == []


class TestFullReport:
    def test_covers_every_item_and_matches_the_cli_shape(self):
        plan = _plan_with_gas_pipe_and_two_trees()
        norms = load_norms()

        report = full_report(plan.project, plan, norms, project_id="project-1", plan_id="plan-1")

        assert report["total_items"] == 2
        assert report["violations"] == 1
        assert report["compliant_items"] == 1
        assert report["project_id"] == "project-1"
        assert "sources" in report and report["sources"]
        assert len(report["items"]) == 2


class TestFullTraceCsv:
    def test_writes_one_row_per_planting_per_constraint(self, tmp_path):
        plan = _plan_with_gas_pipe_and_two_trees()
        norms = load_norms()
        out = tmp_path / "trace.csv"

        row_count = full_trace_csv(plan.project, plan, norms, out)

        assert out.exists()
        with open(out, encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle, delimiter=";"))
        assert len(rows) == row_count
        assert row_count > 0
        assert {"act", "clause", "required_m", "actual_m"} <= set(rows[0])
