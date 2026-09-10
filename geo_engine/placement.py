"""Greedy selection of planting candidates into a final, non-overlapping plan.

geo_engine stays independent of ml_scoring: the scoring function is injected
by the caller (backend service layer) rather than imported here, so either
the heuristic or the ML scoring strategy can be swapped without geo_engine
knowing about ml_scoring at all.
"""

from __future__ import annotations

from typing import Callable

from geo_engine.model import PlantingCandidate, PlantingItem
from geo_engine.norms import PlantingNorms

# (candidate) -> (score in [0, 1], human-readable rationale)
ScoreFn = Callable[[PlantingCandidate], tuple[float, str]]


def greedy_select(
    candidates: list[PlantingCandidate],
    score_fn: ScoreFn,
    norms: PlantingNorms,
    species_by_type: dict[str, str] | None = None,
) -> list[PlantingItem]:
    """Score every candidate, then greedily accept highest-scoring ones first,
    skipping any candidate whose canopy/spacing buffer would overlap an
    already-accepted item.
    """
    species_by_type = species_by_type or {"tree": "default", "shrub": "default", "lawn": "default"}

    scored = [(candidate, *score_fn(candidate)) for candidate in candidates]
    scored.sort(key=lambda t: t[1], reverse=True)

    selected: list[PlantingItem] = []
    selected_footprints = []  # buffered geometries used only for overlap checks

    for candidate, score, rationale in scored:
        spacing = norms.spacing_for(candidate.planting_type)
        footprint = candidate.geometry.buffer(spacing.canopy_radius_m) if candidate.geometry.geom_type == "Point" else candidate.geometry

        if any(footprint.intersects(existing) for existing in selected_footprints):
            continue

        selected.append(
            PlantingItem(
                geometry=candidate.geometry,
                planting_type=candidate.planting_type,
                species=species_by_type.get(candidate.planting_type, "default"),
                score=score,
                rationale=rationale,
            )
        )
        selected_footprints.append(footprint)

    return selected
