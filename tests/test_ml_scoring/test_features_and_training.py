import time

import numpy as np
import pytest
from shapely.geometry import Point

from geo_engine.model import PlantingCandidate
from geo_engine.norms import load_norms
from ml_scoring.features import FEATURE_NAMES, build_existing_greenery_index, compute_features, feature_vector
from ml_scoring.train_synthetic import build_training_set


def test_compute_features_returns_all_names_clamped_to_unit_range(synthetic_scene):
    norms = load_norms()
    candidate = PlantingCandidate(geometry=Point(50, 40), planting_type="tree", clearance_m=2.0, zoning="residential")
    features = compute_features(candidate, norms)

    assert set(features) == set(FEATURE_NAMES)
    assert all(0.0 <= v <= 1.0 for v in features.values())
    assert len(feature_vector(features)) == len(FEATURE_NAMES)


def test_build_training_set_produces_both_classes():
    X, y = build_training_set(n_scenes=2)
    assert isinstance(X, np.ndarray)
    assert X.shape[1] == len(FEATURE_NAMES)
    assert len(y) == X.shape[0]
    assert set(np.unique(y).tolist()) <= {0, 1}


class TestExistingGreeneryIndex:
    """Regression for a live find: scoring against a real street's existing
    greenery (20,004 objects on one pilot street) with the plain
    `min(geom.distance(g) for g in existing_greenery)` loop was still running
    10+ minutes into a single /generate request -- confirmed by benchmark to
    be ~O(candidates x existing_greenery), around 10^9 shapely .distance()
    calls for that one street. build_existing_greenery_index() + STRtree.nearest
    turns the per-candidate cost from O(existing_greenery) into O(log n).
    """

    def test_indexed_and_unindexed_paths_agree(self, synthetic_scene):
        norms = load_norms()
        candidate = PlantingCandidate(geometry=Point(12, 34), planting_type="tree", clearance_m=2.0, zoning="residential")
        existing_greenery = [Point(x, y).buffer(2.0) for x, y in [(0, 0), (100, 100), (10, 40), (500, 500)]]
        index = build_existing_greenery_index(existing_greenery)

        without_index = compute_features(candidate, norms, existing_greenery)
        with_index = compute_features(candidate, norms, existing_greenery, index)

        assert with_index["existing_greenery_gap_score"] == pytest.approx(without_index["existing_greenery_gap_score"])

    def test_no_existing_greenery_still_gives_the_neutral_default(self):
        assert build_existing_greenery_index([]) is None
        assert build_existing_greenery_index(None) is None

        norms = load_norms()
        candidate = PlantingCandidate(geometry=Point(0, 0), planting_type="tree", clearance_m=2.0, zoning="residential")
        features = compute_features(candidate, norms, existing_greenery=[], existing_greenery_index=None)
        assert 0.0 <= features["existing_greenery_gap_score"] <= 1.0

    def test_scoring_thousands_of_candidates_against_a_large_existing_greenery_set_is_fast(self):
        """Not a synthetic worst case -- 20,000 is the real count from one
        pilot street's "Леса и газоны"/"Полоса деревьев" layers. A regression
        here means this bug is back."""
        norms = load_norms()
        existing_greenery = [Point(i % 200, i // 200).buffer(1.0) for i in range(20_000)]
        index = build_existing_greenery_index(existing_greenery)
        candidates = [
            PlantingCandidate(geometry=Point(i * 0.7, i * 0.3), planting_type="tree", clearance_m=2.0, zoning="residential")
            for i in range(3_000)
        ]

        started = time.perf_counter()
        for candidate in candidates:
            compute_features(candidate, norms, existing_greenery, index)
        elapsed = time.perf_counter() - started

        # The naive loop took ~0.1s *per candidate* at this existing_greenery
        # size (measured live) -- 3000 candidates would be five-plus minutes.
        # The indexed path measured well under a second for this; 5s leaves
        # generous headroom for a slower CI machine without letting a real
        # regression back to O(n) slip through unnoticed.
        assert elapsed < 5.0, f"scoring {len(candidates)} candidates took {elapsed:.1f}s -- looks like the O(n) path again"
