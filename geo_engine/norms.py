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
    territory_margin_m: dict[PlantingType, float]
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

    def with_spacing_override(self, planting_type: PlantingType, interval_m: float) -> "PlantingNorms":
        """A copy of these norms with `planting_type`'s spacing replaced by a
        user-chosen interval -- canopy_radius_m is derived as interval_m/2,
        the same 2:1 ratio the shipped tree_default/shrub_default defaults
        already use (5.0/2.5, 3.0/1.5). min_distance_m doubles as the
        candidate grid step in geo_engine.candidates, so it must move
        together with canopy_radius_m: setting only the crown radius while
        leaving a smaller/unrelated grid step is exactly the bug that once
        made shrub density basically uncontrolled (see CLAUDE.md) -- keeping
        the ratio fixed here is what prevents a repeat of that for
        user-supplied values too. Doesn't touch load_norms()'s cache (this
        builds an independent in-memory copy, never re-reads the YAML)."""
        key = f"{planting_type}_default"
        updated = dict(self.species_spacing)
        updated[key] = SpeciesSpacing(min_distance_m=interval_m, canopy_radius_m=interval_m / 2)
        return self.model_copy(update={"species_spacing": updated})

    def territory_margin_for(self, planting_type: PlantingType) -> float:
        """Required clearance in meters from the territory's own outer
        boundary (not from an obstacle inside it -- see buffers.buildable_area,
        which is the only caller). 0.0 fallback, not setback_for's fail-large
        behavior: this project's own planting_norms.yaml always defines all
        three planting types, so a missing key here is a defensive
        edge case, not a real setback this codebase forgot to configure."""
        return self.territory_margin_m.get(planting_type, 0.0)

    def zoning_score(self, zoning: str | None) -> float:
        return self.zoning_suitability.get(zoning or "unknown", self.zoning_suitability["unknown"])


@lru_cache(maxsize=8)
def load_norms(path: Path | str = DEFAULT_NORMS_PATH) -> PlantingNorms:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return PlantingNorms.model_validate(raw)
