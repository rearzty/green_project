"""Orchestrates the geo_engine + ml_scoring pipeline for one "generate plan"
request and stores the result on the in-memory project record. This is the
one place that wires the two independent packages together — neither of them
imports the other.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi.concurrency import run_in_threadpool

from backend.app.core.config import settings
from backend.app.db.models import Plan, PlantingItemRow, Project
from backend.app.services.geo_io import domain_item_to_row, layers_to_domain

from geo_engine.norms import PlantingNorms, load_norms
from geo_engine.planner import plan_items
from geo_engine.territory import MissingTerritoryError, territory_polygon  # noqa: F401  (re-exported by name for existing callers)
from ml_scoring.heuristic_scorer import HeuristicScorer
from ml_scoring.ml_scorer import MLScorer
from ml_scoring.scoring_strategy import ScoringStrategy


class CurrentPlanDeletionError(ValueError):
    """Deleting `is_current` would leave the project with no current plan --
    every other code path here (list_plans's ordering, _find_reusable_plan,
    the frontend's "текущий" badge) assumes exactly one always exists.
    Message is shown to the user as-is."""


def _existing_greenery(zones):
    return [z.geometry for z in zones if z.zone_type == "existing_greenery"]


def build_scorer(scoring_mode: str, norms, existing_greenery) -> ScoringStrategy:
    if scoring_mode == "ml":
        return MLScorer(norms, existing_greenery=existing_greenery, artifact_path=settings.ml_artifact_path)
    return HeuristicScorer(norms, existing_greenery=existing_greenery)


def _effective_norms(norms: PlantingNorms, tree_spacing_m: float | None, shrub_spacing_m: float | None) -> PlantingNorms:
    """Applies a per-generation spacing override on top of the loaded YAML
    norms, if the caller asked for one -- used identically at generation time
    (generate_plan) and at re-materialization time (ensure_materialized) so a
    collapsed-then-reopened plan recomputes with the same spacing it was
    first generated with, instead of silently falling back to the YAML
    default."""
    if tree_spacing_m is not None:
        norms = norms.with_spacing_override("tree", tree_spacing_m)
    if shrub_spacing_m is not None:
        norms = norms.with_spacing_override("shrub", shrub_spacing_m)
    return norms


def _spacing_overridden(tree_spacing_m: float | None, shrub_spacing_m: float | None) -> tuple[str, ...]:
    """Типы, интервал которых задал пользователь.

    Нужно потому, что пользовательский интервал и выведенный из класса кроны
    (МГСН 1.02-02, п. 4.2.9.2) приходят в одно и то же поле `min_distance_m`, и
    внутри planner их уже не различить. Без этого списка явно выставленная в
    панели цифра молча заменялась бы нормативной.
    """
    overridden = []
    if tree_spacing_m:
        overridden.append("tree")
    if shrub_spacing_m:
        overridden.append("shrub")
    return tuple(overridden)


def _compute_planting_rows(
    plan_id: str,
    utilities,
    zones,
    territory,
    planting_types: list[str],
    scorer: ScoringStrategy,
    norms,
    keep_spacing_for: tuple[str, ...] = (),
    on_progress: Callable[[str, int, int], None] | None = None,
) -> list[PlantingItemRow]:
    """Pure CPU work (geometry buffers/candidates/greedy selection) — kept as
    one synchronous function so it can run in a worker thread via
    run_in_threadpool, off the event loop, instead of blocking every other
    request for however long a big territory takes (see CLAUDE.md's
    placement.py/candidates.py performance notes).

    Each planting_type is generated independently from the same
    buildable_area, with no cross-type exclusion -- a tree/shrub candidate
    landing on a lawn candidate's area is expected, not a bug: a tree
    standing in grass is the normal case.

    Point-candidate placement (tree/shrub) is randomized, not gridded (see
    candidates.py) -- seeded from `plan_id`+`planting_type` via zlib.crc32
    (stable across processes/runs, unlike Python's own str hash) so this
    stays a pure function of the plan's recipe: ensure_materialized() calls
    this with the same plan_id and must get back the exact same layout, not
    a fresh random one, when it recomputes a collapsed plan's rows.
    """
    items = plan_items(
        plan_id,
        utilities,
        zones,
        territory,
        planting_types,
        scorer.as_score_fn(),
        norms,
        keep_spacing_for=keep_spacing_for,
        on_progress=on_progress,
    )
    return [domain_item_to_row(plan_id, item) for item in items]


def _attach(plan: Plan, items: list[PlantingItemRow]) -> None:
    """Sets the back-reference every item needs (item.plan.project,
    item.plan.has_manual_edits -- see edit_service.py) -- there's no ORM
    relationship to do this automatically anymore, so whatever puts items
    into a plan does it explicitly, here and in edit_service.restore_items."""
    for item in items:
        item.plan = plan
    plan.items = items


def _prune_stale_plans(project: Project, keep_plan_id: str) -> None:
    """Drop the materialized `items` of every other plan in the project
    that's safe to forget -- not current, no manual edits on it, and not
    already pruned. Safe because `generate_plan` is a pure function of
    (project layers, norms, model artifact, plan.scoring_mode/planting_types)
    -- see Plan's docstring -- so these items are just a cache of something
    `ensure_materialized` can recompute byte-for-byte identical later.
    """
    for plan in project.plans:
        if plan.id == keep_plan_id or not plan.materialized or plan.has_manual_edits:
            continue
        plan.items = []
        plan.materialized = False


def delete_plan(project: Project, plan: Plan) -> None:
    """Permanently removes one plan from history (the panel's own delete
    button, not an edit/undo path -- no restore)."""
    if plan.is_current:
        raise CurrentPlanDeletionError("Нельзя удалить текущий план — переключитесь на другой или сгенерируйте новый.")
    project.plans = [p for p in project.plans if p.id != plan.id]


async def ensure_materialized(plan: Plan) -> Plan:
    """Recompute a pruned plan's `items` on demand (e.g. the user clicks into
    it from plan history). Only ever called on plans `_prune_stale_plans` was
    allowed to prune, i.e. ones with zero manual edits -- so there's no edit
    history to replay on top, just a straight rerun of the same deterministic
    pipeline.
    """
    if plan.materialized:
        return plan

    norms = _effective_norms(load_norms(settings.planting_norms_path), plan.tree_spacing_m, plan.shrub_spacing_m)
    utilities, zones = layers_to_domain(plan.project.layers)
    territory = territory_polygon(zones, utilities)
    existing_greenery = _existing_greenery(zones)
    scorer = build_scorer(plan.scoring_mode, norms, existing_greenery)

    items = await run_in_threadpool(
        _compute_planting_rows,
        plan.id,
        utilities,
        zones,
        territory,
        plan.planting_types,
        scorer,
        norms,
        _spacing_overridden(plan.tree_spacing_m, plan.shrub_spacing_m),
    )
    _attach(plan, items)
    plan.materialized = True
    return plan


def _find_reusable_plan(
    project: Project,
    scoring_mode: str,
    planting_types: list[str],
    tree_spacing_m: float | None,
    shrub_spacing_m: float | None,
) -> Plan | None:
    """If the project's *current* plan already is exactly this recipe
    (same scoring_mode + same set of planting_types + same spacing overrides)
    and nobody has hand-edited it, generating "again" would just create a
    visible duplicate in plan history with identical content. Deliberately
    scoped to the current plan only, not any matching plan anywhere in
    history -- reusing older history would risk resurrecting a plan generated
    under a since-changed planting_norms.yaml/ML model as if it were fresh.
    """
    current = next((p for p in project.plans if p.is_current), None)
    if current is None or current.has_manual_edits or current.scoring_mode != scoring_mode:
        return None
    if set(current.planting_types) != set(planting_types):
        return None
    if current.tree_spacing_m != tree_spacing_m or current.shrub_spacing_m != shrub_spacing_m:
        return None
    return current


async def generate_plan(
    project: Project,
    planting_types: list[str],
    scoring_mode: str,
    tree_spacing_m: float | None = None,
    shrub_spacing_m: float | None = None,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> Plan:
    reusable = _find_reusable_plan(project, scoring_mode, planting_types, tree_spacing_m, shrub_spacing_m)
    if reusable is not None:
        return await ensure_materialized(reusable)

    norms = _effective_norms(load_norms(settings.planting_norms_path), tree_spacing_m, shrub_spacing_m)
    utilities, zones = layers_to_domain(project.layers)
    territory = territory_polygon(zones, utilities)
    existing_greenery = _existing_greenery(zones)
    scorer = build_scorer(scoring_mode, norms, existing_greenery)

    for existing_plan in project.plans:
        existing_plan.is_current = False

    plan = Plan(
        project_id=project.id,
        scoring_mode=scoring_mode,
        planting_types=planting_types,
        tree_spacing_m=tree_spacing_m,
        shrub_spacing_m=shrub_spacing_m,
        is_current=True,
        project=project,
    )

    items = await run_in_threadpool(
        _compute_planting_rows,
        plan.id,
        utilities,
        zones,
        territory,
        planting_types,
        scorer,
        norms,
        _spacing_overridden(tree_spacing_m, shrub_spacing_m),
        on_progress,
    )
    _attach(plan, items)
    plan.item_count = len(items)

    project.plans.append(plan)
    _prune_stale_plans(project, keep_plan_id=plan.id)

    return plan
