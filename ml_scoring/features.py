"""Feature extraction for scoring a PlantingCandidate. Shared by both the
heuristic scorer and the ML scorer so their inputs stay comparable.
"""

from __future__ import annotations

from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from geo_engine.model import PlantingCandidate
from geo_engine.norms import PlantingNorms

# Feature order is significant for MLScorer — train_synthetic.py must produce
# training rows in this exact order. Keep this list as the single source of truth.
FEATURE_NAMES = [
    "extra_clearance_ratio",
    "size_score",
    "compactness",
    "zoning_suitability",
    "existing_greenery_gap_score",
]

REFERENCE_CLEARANCE_M = 5.0  # clearance beyond the minimum norm considered "excellent"
REFERENCE_AREA_M2 = 50.0  # lawn/candidate area considered "large"
REFERENCE_GAP_M = 30.0  # distance from existing greenery considered "a real gap"


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _compactness(geom: BaseGeometry) -> float:
    """Polsby-Popper-style compactness in [0, 1]; 1.0 for a perfect circle,
    near 0 for thin slivers. Points (tree/shrub candidates) are perfectly
    compact by definition.
    """
    if geom.geom_type == "Point":
        return 1.0
    perimeter = geom.length
    if perimeter == 0:
        return 0.0
    return _clamp01((4 * 3.141592653589793 * geom.area) / (perimeter**2))


def build_existing_greenery_index(existing_greenery: list[BaseGeometry] | None) -> STRtree | None:
    """Build once per scorer instance, not per candidate -- see
    _existing_greenery_gap's own docstring for why this matters."""
    return STRtree(existing_greenery) if existing_greenery else None


def _existing_greenery_gap(
    geom: BaseGeometry,
    existing_greenery: list[BaseGeometry],
    index: STRtree | None = None,
) -> float:
    """Distance to the closest existing planting, or REFERENCE_GAP_M (a
    neutral default, not a penalty) when there's nothing to compare against.

    `index`, when given, is an STRtree over `existing_greenery` built once by
    the caller -- pass it whenever this runs in a loop over many candidates.
    Without it this is `min(geom.distance(g) for g in existing_greenery)`,
    O(existing_greenery) *per candidate*: fine for a handful of existing
    plantings, but a real street can carry tens of thousands (one pilot
    street: 20,004 "Леса и газоны"/"Полоса деревьев" objects) -- scoring tens
    of thousands of raw candidates against that with a plain Python loop is
    O(candidates x existing_greenery), confirmed live to still be running
    after 10+ minutes on exactly that data (the arithmetic: ~36k candidates x
    20k geometries is on the order of 10^9 shapely .distance() calls). The
    indexed path is one O(log n) nearest-neighbour lookup per candidate.
    """
    if index is not None:
        idx = index.nearest(geom)
        if idx is None:
            return REFERENCE_GAP_M
        return geom.distance(existing_greenery[int(idx)])
    if not existing_greenery:
        return REFERENCE_GAP_M
    return min(geom.distance(g) for g in existing_greenery)


def compute_features(
    candidate: PlantingCandidate,
    norms: PlantingNorms,
    existing_greenery: list[BaseGeometry] | None = None,
    existing_greenery_index: STRtree | None = None,
) -> dict[str, float]:
    existing_greenery = existing_greenery or []

    clearance = candidate.clearance_m if candidate.clearance_m != float("inf") else REFERENCE_CLEARANCE_M
    extra_clearance_ratio = _clamp01(clearance / REFERENCE_CLEARANCE_M)

    size = candidate.area_m2 if candidate.area_m2 is not None else norms.spacing_for(candidate.planting_type).canopy_radius_m ** 2
    size_score = _clamp01(size / REFERENCE_AREA_M2)

    compactness = _compactness(candidate.geometry)
    zoning_suitability = norms.zoning_score(candidate.zoning)

    gap = _existing_greenery_gap(candidate.geometry, existing_greenery, existing_greenery_index)
    existing_greenery_gap_score = _clamp01(gap / REFERENCE_GAP_M)

    return {
        "extra_clearance_ratio": extra_clearance_ratio,
        "size_score": size_score,
        "compactness": compactness,
        "zoning_suitability": zoning_suitability,
        "existing_greenery_gap_score": existing_greenery_gap_score,
    }


def feature_vector(features: dict[str, float]) -> list[float]:
    return [features[name] for name in FEATURE_NAMES]
