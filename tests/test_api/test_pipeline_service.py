"""Unit tests for pipeline_service.py's pure (non-DB) functions:
_find_reusable_plan's reuse-vs-regenerate decision, and _effective_norms'
per-generation spacing override.
"""

from __future__ import annotations

import asyncio

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
    """Only the is_current guard is unit-testable without a real DB session --
    it's checked before delete_plan ever touches `session`, so this test
    never opens one. The actual DELETE statements are exercised end-to-end
    against a live Postgres in manual/browser verification, not here."""

    def test_refuses_to_delete_the_current_plan(self):
        plan = _plan(is_current=True)
        with pytest.raises(CurrentPlanDeletionError):
            asyncio.run(delete_plan(session=None, plan=plan))  # type: ignore[arg-type]


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
