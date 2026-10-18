"""Модель «где бы посадил проектировщик» (ml_scoring/designer_model.py) и то,
как генератор её слушается: отрицательная оценка — место отклонено."""

from __future__ import annotations

import joblib
import numpy as np
import pytest
from shapely.geometry import LineString, Point, box
from sklearn.ensemble import HistGradientBoostingClassifier

from geo_engine.model import PlantingCandidate, Zone
from geo_engine.norms import load_norms
from geo_engine.placement import greedy_select
from ml_scoring.designer_model import FEATURE_NAMES, MAX_DISTANCE_M, DesignerFeatures, DesignerScorer


def _scene():
    territory = box(0, 0, 100, 40)
    zones = [
        Zone(LineString([(0, 0), (100, 0)]), "road", {"layer": "БР 1000 300 150"}),
        Zone(box(0, 30, 100, 40), "building", {}),
        Zone(Point(50, 10), "existing_greenery", {}),
    ]
    return territory, zones


def test_features_measure_distances_to_the_drawing():
    territory, zones = _scene()
    X = DesignerFeatures([], zones, territory).matrix(np.array([[50.0, 4.0]]))
    row = dict(zip(FEATURE_NAMES, X[0]))
    assert row["d_road"] == pytest.approx(4.0)
    assert row["d_building"] == pytest.approx(26.0)
    assert row["d_greenery"] == pytest.approx(6.0)
    assert row["d_edge"] == pytest.approx(4.0)
    # Сетей нет — расстояние упирается в потолок, а не в ноль.
    assert row["d_pipe"] == MAX_DISTANCE_M
    assert row["n_greenery_10"] == 1


def _artifact(tmp_path, gate):
    """Модель, которая любит места у дороги (d_road < 5) — чтобы проверить
    отсечение, не завися от реального артефакта."""
    rng = np.random.default_rng(0)
    X = rng.uniform(0, 30, size=(400, len(FEATURE_NAMES)))
    y = (X[:, FEATURE_NAMES.index("d_road")] < 5).astype(int)
    model = HistGradientBoostingClassifier(max_iter=50, random_state=0).fit(X, y)
    path = tmp_path / "designer.joblib"
    joblib.dump({"all": model, "gate": gate, "features": list(FEATURE_NAMES)}, path)
    return path


def _candidates():
    return [
        PlantingCandidate(Point(20, 3), "tree", clearance_m=1.0),
        PlantingCandidate(Point(70, 20), "tree", clearance_m=1.0),
    ]


def test_places_unlike_a_designers_choice_are_rejected_below_the_gate(tmp_path):
    territory, zones = _scene()
    scorer = DesignerScorer(load_norms(), [], zones, territory, artifact_path=_artifact(tmp_path, gate=0.5))
    near_road, far = scorer.score_batch(_candidates())
    assert near_road[0] > 0.5
    assert far[0] < 0
    selected = greedy_select(_candidates(), scorer.as_score_fn(), load_norms())
    assert [(i.geometry.x, i.geometry.y) for i in selected] == [(20, 3)]


def test_without_a_gate_nothing_is_rejected(tmp_path):
    territory, zones = _scene()
    scorer = DesignerScorer(load_norms(), [], zones, territory, artifact_path=_artifact(tmp_path, gate=None))
    assert all(score >= 0 for score, _ in scorer.score_batch(_candidates()))
    assert len(greedy_select(_candidates(), scorer.as_score_fn(), load_norms())) == 2


def test_shrubs_and_lawn_fall_back_to_the_heuristic(tmp_path):
    territory, zones = _scene()
    scorer = DesignerScorer(load_norms(), [], zones, territory, artifact_path=_artifact(tmp_path, gate=0.99))
    (score, rationale), = scorer.score_batch([PlantingCandidate(Point(70, 20), "shrub", clearance_m=1.0)])
    assert score >= 0
    assert "проектн" not in rationale


def test_greedy_select_skips_negative_scores_only():
    norms = load_norms()
    candidates = _candidates()
    keep_all = greedy_select(candidates, lambda cs: [(0.0, "") for _ in cs], norms)
    assert len(keep_all) == 2
    reject_one = greedy_select(candidates, lambda cs: [(1.0, ""), (-1.0, "")], norms)
    assert len(reject_one) == 1


def test_the_gate_switches_off_on_a_territory_the_model_rejects_entirely(tmp_path):
    """Территория, непохожая на улицы пилота (синтетика): с отсечением план
    остался бы без деревьев, поэтому модель там только ранжирует."""
    territory, zones = _scene()
    far_from_roads = [z for z in zones if z.zone_type != "road"]  # без дороги модель не пропустит ничего
    scorer = DesignerScorer(load_norms(), [], far_from_roads, territory, artifact_path=_artifact(tmp_path, gate=0.5))
    assert scorer.gate_pass_share == 0.0
    assert all(score >= 0 for score, _ in scorer.score_batch(_candidates()))
