"""Train the first iteration of MLScorer on weak-labeled synthetic data.

Methodology (stated plainly for the presentation, not hidden): with no real
labeled placements available before 2026-09-15, we generate several synthetic
territories, run the same heuristic scorer used as the baseline strategy over
their candidates, and treat "heuristic score above a threshold, plus label
noise" as a weak label. This gives a real, inspectable sklearn artifact and a
real inference path today; the plan is to re-train on actual designer
feedback/corrections once the real dataset and a working prototype exist.

Run: python -m ml_scoring.train_synthetic
"""

from __future__ import annotations

from pathlib import Path

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from geo_engine.buffers import build_exclusion_zone, buildable_area
from geo_engine.candidates import generate_candidates
from geo_engine.norms import load_norms
from ml_scoring.features import compute_features, feature_vector
from ml_scoring.heuristic_scorer import HeuristicScorer
from scripts.generate_synthetic_data import generate_synthetic_territory

ARTIFACT_PATH = Path(__file__).parent / "artifacts" / "model.joblib"
PLANTING_TYPES = ["tree", "shrub", "lawn"]
N_SCENES = 12
WEAK_LABEL_THRESHOLD = 0.55
LABEL_NOISE_STD = 0.08


def _existing_greenery(zones) -> list:
    return [z.geometry for z in zones if z.zone_type == "existing_greenery"]


def build_training_set(n_scenes: int = N_SCENES, seed: int = 42) -> tuple[np.ndarray, np.ndarray]:
    norms = load_norms()
    rng = np.random.default_rng(seed)

    rows: list[list[float]] = []
    labels: list[int] = []

    for scene_seed in range(n_scenes):
        scene = generate_synthetic_territory(seed=scene_seed)
        territory, utilities, zones = scene["territory"], scene["utilities"], scene["zones"]
        existing_greenery = _existing_greenery(zones)
        heuristic = HeuristicScorer(norms, existing_greenery=existing_greenery)

        for planting_type in PLANTING_TYPES:
            exclusion = build_exclusion_zone(utilities, zones, planting_type, norms)
            buildable = buildable_area(territory, exclusion, zones)
            candidates = generate_candidates(buildable, exclusion, planting_type, norms, zoning_zones=zones)

            for candidate in candidates:
                features = compute_features(candidate, norms, existing_greenery)
                heuristic_score, _ = heuristic.score(candidate)
                noisy_score = heuristic_score + rng.normal(0, LABEL_NOISE_STD)
                label = int(noisy_score >= WEAK_LABEL_THRESHOLD)

                rows.append(feature_vector(features))
                labels.append(label)

    return np.array(rows), np.array(labels)


def train_and_save(artifact_path: Path = ARTIFACT_PATH) -> dict:
    X, y = build_training_set()
    if len(set(y.tolist())) < 2:
        raise RuntimeError(
            "Weak labels came out single-class — widen WEAK_LABEL_THRESHOLD or generate more scenes."
        )

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)

    pipeline = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000))
    pipeline.fit(X_train, y_train)
    accuracy = pipeline.score(X_test, y_test)

    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(pipeline, artifact_path)

    return {"n_samples": len(y), "test_accuracy": accuracy, "artifact_path": str(artifact_path)}


if __name__ == "__main__":
    result = train_and_save()
    print(f"Trained on {result['n_samples']} synthetic candidates.")
    print(f"Held-out weak-label accuracy: {result['test_accuracy']:.3f}")
    print(f"Saved model to {result['artifact_path']}")
