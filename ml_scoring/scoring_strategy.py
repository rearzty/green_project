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
        """Return (score in [0, 1], human-readable rationale)."""
        raise NotImplementedError

    def as_score_fn(self):
        """Adapter matching geo_engine.placement.ScoreFn's Callable signature."""
        return self.score
