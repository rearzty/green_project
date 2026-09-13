"""Orchestrates the geo_engine + ml_scoring pipeline for one "generate plan"
request and persists the result. This is the one place that wires the two
independent packages together — neither of them imports the other.
"""

from __future__ import annotations

import random
import zlib

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core.config import settings
from backend.app.db.models import Plan, PlantingItemRow, Project
from backend.app.services.geo_io import domain_item_to_row_values, layers_to_domain
from geo_engine.buffers import build_exclusion_zone, buildable_area
from geo_engine.candidates import generate_candidates
from geo_engine.norms import PlantingNorms, load_norms
from geo_engine.placement import greedy_select
from geo_engine.species import load_species
from ml_scoring.heuristic_scorer import HeuristicScorer
from ml_scoring.ml_scorer import MLScorer
from ml_scoring.scoring_strategy import ScoringStrategy


class MissingTerritoryError(ValueError):
    """The uploaded file has no territory boundary zone -- nothing can be
    planned or placed without one. Message is shown to the user as-is."""


def territory_polygon(zones):
    """Not private (despite the rest of this module's helpers) -- edit_service
    also needs it, to reject manual edits that would place a point outside
    the project's own territory boundary."""
    for zone in zones:
        if zone.zone_type == "territory":
            return zone.geometry
    raise MissingTerritoryError("В загруженном файле нет границы участка (слой territory / BOUNDARY) — план построить нельзя.")


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
    default (the exact "forgot the second call site" bug this session's own
    territory-tolerance fix hit once already, see CLAUDE.md)."""
    if tree_spacing_m is not None:
        norms = norms.with_spacing_override("tree", tree_spacing_m)
    if shrub_spacing_m is not None:
        norms = norms.with_spacing_override("shrub", shrub_spacing_m)
    return norms


def _compute_planting_rows(
    plan_id: str,
    utilities,
    zones,
    territory,
    planting_types: list[str],
    scorer: ScoringStrategy,
    norms,
) -> list[dict]:
    """Pure CPU work (geometry buffers/candidates/greedy selection, no DB
    access) — kept as one synchronous function so it can run in a worker
    thread via run_in_threadpool, off the event loop, instead of blocking
    every other request for however long a big territory takes (see
    CLAUDE.md's placement.py/candidates.py performance notes). Returns plain
    dicts (Core bulk-insert values), not ORM objects -- see generate_plan.

    Each planting_type is generated independently from the same
    buildable_area, with no cross-type exclusion -- a tree/shrub candidate
    landing on a lawn candidate's area is expected, not a bug: a tree
    standing in grass is the normal case (a cutout in pavement around a
    trunk is the rare exception, not something this models). Trimming the
    lawn's shape around what actually got planted, if wanted, is a separate,
    later, user-driven editing feature, not something generation itself
    should enforce.

    Point-candidate placement (tree/shrub) is randomized, not gridded (see
    candidates.py) -- seeded from `plan_id`+`planting_type` via zlib.crc32
    (stable across processes/runs, unlike Python's own str hash) so this
    stays a pure function of the plan's recipe: ensure_materialized() calls
    this with the same plan_id and must get back the exact same layout, not
    a fresh random one, when it recomputes a collapsed plan's rows.
    """
    rows: list[dict] = []
    score_fn = scorer.as_score_fn()
    species = load_species()
    for planting_type in planting_types:
        exclusion = build_exclusion_zone(utilities, zones, planting_type, norms)
        margin = norms.territory_margin_for(planting_type)
        buildable = buildable_area(territory, exclusion, zones, territory_margin_m=margin)
        seed = zlib.crc32(f"{plan_id}:{planting_type}".encode())
        candidates = generate_candidates(buildable, exclusion, planting_type, norms, zoning_zones=zones, seed=seed)
        items = greedy_select(candidates, score_fn, norms)

        species_pool = species.get(planting_type)
        if species_pool:
            # A seed distinct from the candidate-scatter one above -- species
            # assignment shouldn't be even conceptually tied to placement
            # randomness. Same reproducibility requirement either way:
            # ensure_materialized() must re-assign the same species to the
            # same items, not a fresh random pick.
            species_rng = random.Random(zlib.crc32(f"{plan_id}:{planting_type}:species".encode()))
            for item in items:
                item.species = species_rng.choice(species_pool)

        rows.extend(domain_item_to_row_values(plan_id, item) for item in items)
    return rows


async def _prune_stale_plans(session: AsyncSession, project: Project, keep_plan_id: str) -> None:
    """Drop the materialized `planting_items` rows of every other plan in the
    project that's safe to forget -- not current, no manual edits on it, and
    not already pruned. Safe because `generate_plan` is a pure function of
    (project layers, norms, model artifact, plan.scoring_mode/planting_types)
    -- see Plan's docstring -- so these rows are just a cache of something
    `ensure_materialized` can recompute byte-for-byte identical later. This
    runs on every generate, not just once, so plan history doesn't
    accumulate an ever-growing pile of full-resolution rows from repeated
    testing/tuning clicks -- only ever the current plan, plus however many
    plans a human has actually hand-edited, are materialized at once.
    """
    for plan in project.plans:
        if plan.id == keep_plan_id or not plan.materialized or plan.has_manual_edits:
            continue
        await session.execute(delete(PlantingItemRow).where(PlantingItemRow.plan_id == plan.id))
        plan.materialized = False


async def _reload_with_items(session: AsyncSession, plan_id: str) -> Plan:
    # populate_existing=True is load-bearing here, not decoration: the Plan
    # identity-mapped in this session may already have plan.items loaded --
    # possibly as empty, e.g. from the query that found materialized=False --
    # and expire_on_commit=False (db/session.py) means commit() doesn't
    # invalidate that. Without it, selectinload sees Plan.items as "already
    # loaded" for this instance and skips re-querying it, silently handing
    # back stale (often empty) data even though fresh rows just landed in the
    # DB. Bit us once already (ensure_materialized) -- see CLAUDE.md.
    result = await session.execute(
        select(Plan)
        .where(Plan.id == plan_id)
        .options(selectinload(Plan.items), selectinload(Plan.project))
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


async def ensure_materialized(session: AsyncSession, plan: Plan) -> Plan:
    """Recompute and re-insert a pruned plan's `planting_items` rows on
    demand (e.g. the user clicks into it from plan history). Only ever
    called on plans `_prune_stale_plans` was allowed to prune, i.e. ones
    with zero manual edits -- so there's no edit history to replay on top,
    just a straight rerun of the same deterministic pipeline.
    """
    if plan.materialized:
        return plan

    norms = _effective_norms(load_norms(settings.planting_norms_path), plan.tree_spacing_m, plan.shrub_spacing_m)
    utilities, zones = layers_to_domain(plan.project.layers)
    territory = territory_polygon(zones)
    existing_greenery = _existing_greenery(zones)
    scorer = build_scorer(plan.scoring_mode, norms, existing_greenery)

    rows = await run_in_threadpool(
        _compute_planting_rows, plan.id, utilities, zones, territory, plan.planting_types, scorer, norms
    )
    if rows:
        await session.execute(insert(PlantingItemRow), rows)
    plan.materialized = True
    await session.commit()
    return await _reload_with_items(session, plan.id)


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
    visible duplicate in plan history with identical content -- nothing new
    to show, just clutter from re-clicking the same button. Deliberately
    scoped to the current plan only (not any matching plan anywhere in
    history): reusing older history would risk resurrecting a plan generated
    under a since-changed planting_norms.yaml/ML model as if it were fresh,
    which the recipe-replay mechanism (see Plan's docstring) doesn't version
    against.
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
    session: AsyncSession,
    project: Project,
    planting_types: list[str],
    scoring_mode: str,
    tree_spacing_m: float | None = None,
    shrub_spacing_m: float | None = None,
) -> Plan:
    reusable = _find_reusable_plan(project, scoring_mode, planting_types, tree_spacing_m, shrub_spacing_m)
    if reusable is not None:
        reusable = await ensure_materialized(session, reusable)
        return await _reload_with_items(session, reusable.id)

    norms = _effective_norms(load_norms(settings.planting_norms_path), tree_spacing_m, shrub_spacing_m)
    utilities, zones = layers_to_domain(project.layers)
    territory = territory_polygon(zones)
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
    )
    session.add(plan)
    await session.flush()  # assigns plan.id (client-side default, no DB round trip needed), needed by _compute_planting_rows below

    rows = await run_in_threadpool(_compute_planting_rows, plan.id, utilities, zones, territory, planting_types, scorer, norms)
    if rows:
        # Core bulk INSERT, not session.add_all() -- a real territory can
        # produce thousands of planting items per request, and individually
        # add()-ing/flushing each as an ORM object measured at ~2.5ms/row
        # (no statement batching applied) vs. a fraction of a second for one
        # bulk statement regardless of row count (measured: 2746 rows,
        # 7.6s ORM vs well under 1s bulk).
        await session.execute(insert(PlantingItemRow), rows)
    plan.item_count = len(rows)

    await _prune_stale_plans(session, project, keep_plan_id=plan.id)

    await session.commit()

    # Re-fetch with plan.items/plan.project eagerly loaded before handing the
    # object back to callers (_to_plan_out reads both). Assigning them
    # in-process instead (plan.project = project, plan.items = rows) sounds
    # simpler but both a collection-relationship assignment and reading back
    # an unset one need to load the *current* DB state first for
    # cascade/diffing purposes -- that load needs an await that only works
    # inside SQLAlchemy's async "greenlet" context, which plain attribute
    # access from outside an awaited session call isn't, raising
    # MissingGreenlet. A plain query sidesteps the whole question.
    return await _reload_with_items(session, plan.id)
