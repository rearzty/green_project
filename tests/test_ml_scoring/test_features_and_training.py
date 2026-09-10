import numpy as np

from ml_scoring.features import FEATURE_NAMES, compute_features, feature_vector
from ml_scoring.train_synthetic import build_training_set


def test_compute_features_returns_all_names_clamped_to_unit_range(synthetic_scene):
    from geo_engine.model import PlantingCandidate
    from geo_engine.norms import load_norms
    from shapely.geometry import Point

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
