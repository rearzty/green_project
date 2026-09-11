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

# (all candidates) -> (score in [0, 1], human-readable rationale) per candidate,
# same order as the input. Batch-shaped (not one call per candidate) so an
# ML-backed strategy can run one vectorized inference call over everything
# instead of thousands of individual ones -- measured ~3x slower end-to-end
# scoring one candidate at a time on a modest ~5,000-candidate territory.
ScoreFn = Callable[[list[PlantingCandidate]], list[tuple[float, str]]]

# A footprint whose bounding box would hash into more grid cells than this is
# cheaper to check with a direct scan than to bucket — keeps the rare large
# (e.g. whole-buildable-area lawn) footprint fast without needing a separate
# code path from the many small, uniformly-sized tree/shrub canopy circles.
_MAX_CELLS_PER_FOOTPRINT = 64


def _cell_index(x: float, y: float, cell_size: float) -> tuple[int, int]:
    return int(x // cell_size), int(y // cell_size)


def _cells_for_bounds(bounds: tuple[float, float, float, float], cell_size: float) -> list[tuple[int, int]]:
    minx, miny, maxx, maxy = bounds
    x0, y0 = _cell_index(minx, miny, cell_size)
    x1, y1 = _cell_index(maxx, maxy, cell_size)
    return [(cx, cy) for cx in range(x0, x1 + 1) for cy in range(y0, y1 + 1)]


def greedy_select(
    candidates: list[PlantingCandidate],
    score_fn: ScoreFn,
    norms: PlantingNorms,
    species_by_type: dict[str, str] | None = None,
) -> list[PlantingItem]:
    """Score every candidate, then greedily accept highest-scoring ones first,
    skipping any candidate whose canopy/spacing buffer would overlap an
    already-accepted item.

    Overlap checks go through a uniform spatial hash grid keyed by each
    footprint's bounding box, so a candidate is only compared against
    already-accepted footprints that are actually nearby. A plain "check
    against every accepted item so far" scan is O(n * k) — fine on the
    ~350-candidate synthetic 100x80m demo territory, but measured at ~30s per
    5,000 candidates (i.e. hours) on a realistic city-block-scale territory
    with hundreds of thousands of grid candidates. A handful of footprints
    too large to bucket usefully (e.g. a whole-buildable-area lawn polygon)
    fall back to a direct scan against each other instead — there are always
    few of those, so the scan stays cheap.
    """
    species_by_type = species_by_type or {"tree": "default", "shrub": "default", "lawn": "default"}

    scores_and_rationales = score_fn(candidates)
    scored = [(candidate, score, rationale) for candidate, (score, rationale) in zip(candidates, scores_and_rationales)]
    scored.sort(key=lambda t: t[1], reverse=True)

    selected: list[PlantingItem] = []
    indexed_footprints: list = []  # footprints small enough to live in the grid
    large_footprints: list = []  # footprints too large to bucket usefully (rare)
    grid: dict[tuple[int, int], list[int]] = {}

    for candidate, score, rationale in scored:
        spacing = norms.spacing_for(candidate.planting_type)
        footprint = candidate.geometry.buffer(spacing.canopy_radius_m) if candidate.geometry.geom_type == "Point" else candidate.geometry

        cell_size = max(spacing.canopy_radius_m * 2, 1.0)
        cells = _cells_for_bounds(footprint.bounds, cell_size)
        fits_grid = len(cells) <= _MAX_CELLS_PER_FOOTPRINT

        if any(footprint.intersects(existing) for existing in large_footprints):
            continue
        if fits_grid:
            nearby = {idx for cell in cells for idx in grid.get(cell, ())}
            if any(footprint.intersects(indexed_footprints[idx]) for idx in nearby):
                continue
        elif any(footprint.intersects(existing) for existing in indexed_footprints):
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

        if fits_grid:
            idx = len(indexed_footprints)
            indexed_footprints.append(footprint)
            for cell in cells:
                grid.setdefault(cell, []).append(idx)
        else:
            large_footprints.append(footprint)

    return selected
