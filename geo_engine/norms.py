"""Loader for the configurable planting-setback rulebook (planting_norms.yaml)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel

DEFAULT_NORMS_PATH = Path(__file__).parent / "config" / "planting_norms.yaml"

PlantingType = str  # "tree" | "shrub" | "lawn"


class SpeciesSpacing(BaseModel):
    min_distance_m: float
    canopy_radius_m: float


class PlantingNorms(BaseModel):
    setbacks_m: dict[str, dict[PlantingType, float]]
    species_spacing: dict[str, SpeciesSpacing]
    min_candidate_area_m2: dict[PlantingType, float]
    zoning_suitability: dict[str, float]

    def setback_for(self, utility_type: str, planting_type: PlantingType) -> float:
        """Required clearance in meters; unknown utility types fail safe to the
        largest known setback for that planting type rather than 0."""
        rules = self.setbacks_m.get(utility_type)
        if rules is not None and planting_type in rules:
            return rules[planting_type]
        known = [r[planting_type] for r in self.setbacks_m.values() if planting_type in r]
        return max(known) if known else 2.0

    def spacing_for(self, planting_type: PlantingType) -> SpeciesSpacing:
        key = f"{planting_type}_default"
        return self.species_spacing.get(key, SpeciesSpacing(min_distance_m=2.0, canopy_radius_m=1.0))

    def zoning_score(self, zoning: str | None) -> float:
        return self.zoning_suitability.get(zoning or "unknown", self.zoning_suitability["unknown"])


@lru_cache(maxsize=8)
def load_norms(path: Path | str = DEFAULT_NORMS_PATH) -> PlantingNorms:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return PlantingNorms.model_validate(raw)
