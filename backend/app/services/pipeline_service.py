"""Orchestrates the geo_engine + ml_scoring pipeline for one "generate plan"
request and persists the result. This is the one place that wires the two
independent packages together — neither of them imports the other.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.app.core.config import settings
from backend.app.db.models import Plan, Project
from backend.app.services.geo_io import domain_item_to_row, layers_to_domain
from geo_engine.buffers import build_exclusion_zone, buildable_area
from geo_engine.candidates import generate_candidates
from geo_engine.norms import load_norms
from geo_engine.placement import greedy_select
from ml_scoring.heuristic_scorer import HeuristicScorer
from ml_scoring.ml_scorer import MLScorer
from ml_scoring.scoring_strategy import ScoringStrategy


def _territory_polygon(zones):
    for zone in zones:
        if zone.zone_type == "territory":
            return zone.geometry
    raise ValueError("Project has no 'territory' zone layer — cannot generate a plan.")


def _existing_greenery(zones):
    return [z.geometry for z in zones if z.zone_type == "existing_greenery"]


def build_scorer(scoring_mode: str, norms, existing_greenery) -> ScoringStrategy:
    if scoring_mode == "ml":
        return MLScorer(norms, existing_greenery=existing_greenery, artifact_path=settings.ml_artifact_path)
    return HeuristicScorer(norms, existing_greenery=existing_greenery)


def generate_plan(session: Session, project: Project, planting_types: list[str], scoring_mode: str) -> Plan:
    norms = load_norms(settings.planting_norms_path)
    utilities, zones = layers_to_domain(project.layers)
    territory = _territory_polygon(zones)
    existing_greenery = _existing_greenery(zones)
    scorer = build_scorer(scoring_mode, norms, existing_greenery)

    for existing_plan in project.plans:
        existing_plan.is_current = False

    plan = Plan(project_id=project.id, scoring_mode=scoring_mode, is_current=True)
    session.add(plan)
    session.flush()  # assigns plan.id, needed by domain_item_to_row below

    for planting_type in planting_types:
        exclusion = build_exclusion_zone(utilities, zones, planting_type, norms)
        buildable = buildable_area(territory, exclusion, zones)
        candidates = generate_candidates(buildable, exclusion, planting_type, norms, zoning_zones=zones)
        items = greedy_select(candidates, scorer.as_score_fn(), norms)
        for item in items:
            session.add(domain_item_to_row(plan.id, item))

    session.commit()
    session.refresh(plan)
    return plan
