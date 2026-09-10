"""Explainable weighted-sum scoring — the honest baseline "analytical model".

Weights are configuration, not code, so they can be tuned/justified in the
presentation without touching Python. Every score comes with a plain-language
rationale citing the dominant factor(s), which matters for presenting this as
a transparent decision-support tool rather than a black box.
"""

from __future__ import annotations

from shapely.geometry.base import BaseGeometry

from geo_engine.model import PlantingCandidate
from geo_engine.norms import PlantingNorms
from ml_scoring.features import compute_features
from ml_scoring.scoring_strategy import ScoringStrategy

DEFAULT_WEIGHTS = {
    "extra_clearance_ratio": 0.30,
    "size_score": 0.20,
    "compactness": 0.15,
    "zoning_suitability": 0.20,
    "existing_greenery_gap_score": 0.15,
}

FEATURE_LABELS_RU = {
    "extra_clearance_ratio": "запас по отступу от сетей",
    "size_score": "площадь/размер площадки",
    "compactness": "компактность формы",
    "zoning_suitability": "пригодность зонирования",
    "existing_greenery_gap_score": "удалённость от существующего озеленения",
}


class HeuristicScorer(ScoringStrategy):
    def __init__(
        self,
        norms: PlantingNorms,
        weights: dict[str, float] | None = None,
        existing_greenery: list[BaseGeometry] | None = None,
    ):
        self.norms = norms
        self.weights = weights or DEFAULT_WEIGHTS
        self.existing_greenery = existing_greenery or []

    def score(self, candidate: PlantingCandidate) -> tuple[float, str]:
        features = compute_features(candidate, self.norms, self.existing_greenery)
        weighted = {name: features[name] * self.weights.get(name, 0.0) for name in features}
        total = sum(weighted.values())

        top_factor = max(weighted, key=weighted.get)
        rationale = (
            f"Эвристический скоринг: {total:.2f}. "
            f"Основной положительный фактор — {FEATURE_LABELS_RU[top_factor]} "
            f"({features[top_factor]:.2f})."
        )
        return total, rationale
