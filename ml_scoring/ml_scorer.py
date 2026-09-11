"""ML scoring strategy: a scikit-learn classifier trained on weak-labeled
synthetic data (see train_synthetic.py), predicting P(suitable) as the score.

This is deliberately honest about its provenance: with no real labeled data
available before the hackathon's real dataset opens (15.09), the model is
trained on synthetically-generated territories whose labels are derived from
the heuristic scorer plus noise (weak supervision). It's a real trained
artifact with a real inference path — presented as a first iteration meant to
be re-trained on real designer feedback once available, not as a finished
supervised model.
"""

from __future__ import annotations

from pathlib import Path

import joblib
from shapely.geometry.base import BaseGeometry

from geo_engine.model import PlantingCandidate
from geo_engine.norms import PlantingNorms
from ml_scoring.features import compute_features, feature_vector
from ml_scoring.scoring_strategy import ScoringStrategy

DEFAULT_ARTIFACT_PATH = Path(__file__).parent / "artifacts" / "model.joblib"


class ModelNotTrainedError(RuntimeError):
    pass


class MLScorer(ScoringStrategy):
    def __init__(
        self,
        norms: PlantingNorms,
        existing_greenery: list[BaseGeometry] | None = None,
        artifact_path: Path | str = DEFAULT_ARTIFACT_PATH,
    ):
        artifact_path = Path(artifact_path)
        if not artifact_path.exists():
            raise ModelNotTrainedError(
                f"No trained model at {artifact_path}. Run `python -m ml_scoring.train_synthetic` first."
            )
        self.pipeline = joblib.load(artifact_path)
        self.norms = norms
        self.existing_greenery = existing_greenery or []

    def score(self, candidate: PlantingCandidate) -> tuple[float, str]:
        return self.score_batch([candidate])[0]

    def score_batch(self, candidates: list[PlantingCandidate]) -> list[tuple[float, str]]:
        if not candidates:
            return []
        vectors = [feature_vector(compute_features(c, self.norms, self.existing_greenery)) for c in candidates]
        # One vectorized sklearn call over the whole batch, not one Python
        # call per candidate -- see ScoringStrategy.score_batch.
        probabilities = self.pipeline.predict_proba(vectors)[:, 1]
        return [
            (float(p), f"ML-модель (обучена на синтетических данных): вероятность пригодности {p:.2f}.")
            for p in probabilities
        ]
