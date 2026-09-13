"""Loader for the cosmetic species-name pool (species.yaml). Deliberately its
own tiny module, not a field on PlantingNorms -- this isn't a setback rule,
just a placeholder list of realistic-looking names to assign per planted
item (see pipeline_service.py::_compute_planting_rows), same spirit as
planting_norms.yaml's own draft values (CLAUDE.md's "Плейсхолдеры" section).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

DEFAULT_SPECIES_PATH = Path(__file__).parent / "config" / "species.yaml"


@lru_cache(maxsize=8)
def load_species(path: Path | str = DEFAULT_SPECIES_PATH) -> dict[str, list[str]]:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return {planting_type: list(names) for planting_type, names in raw.items()}
