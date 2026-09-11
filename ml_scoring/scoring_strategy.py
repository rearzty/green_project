"""Common interface both scoring implementations satisfy, so the backend
service layer and geo_engine.placement.greedy_select can swap between them
via config (`scoring_mode: "heuristic" | "ml"`) without caring which one it is.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from geo_engine.model import PlantingCandidate


class ScoringStrategy(ABC):
    @abstractmethod
    def score(self, candidate: PlantingCandidate) -> tuple[float, str]:
        """Score a single candidate: (score in [0, 1], rationale)."""
        raise NotImplementedError

    def score_batch(self, candidates: list[PlantingCandidate]) -> list[tuple[float, str]]:
        """Score every candidate, same order as the input. Default just
        loops `score()` one at a time -- fine for HeuristicScorer's cheap
        pure-Python weighted sum. Override when scoring as a batch is
        meaningfully faster than one-at-a-time (MLScorer: a single
        vectorized sklearn `predict_proba()` call over the whole batch vs.
        one Python-level call per candidate, measured ~3x slower end-to-end
        on a modest ~5,000-candidate territory)."""
        return [self.score(candidate) for candidate in candidates]

    def as_score_fn(self):
        """Adapter matching geo_engine.placement.ScoreFn's batch-shaped signature."""
        return self.score_batch
