"""The planting pipeline itself: territory + constraints -> selected plantings.

Pure geometry and scoring, no database and no web framework — that is the point.
The backend used to own this loop inside `pipeline_service._compute_planting_rows`,
which meant the only way to run the project's core algorithm was to bring up
PostGIS and FastAPI. The brief asks for a runnable DXF -> DXF pipeline and
explicitly accepts a CLI as the delivery, so the algorithm lives here and both
callers — the CLI and the backend — drive the same code.
"""

from __future__ import annotations

import random
import zlib
from collections.abc import Callable

from shapely.geometry.base import BaseGeometry

from geo_engine.buffers import build_exclusion_zone, buildable_area
from geo_engine.candidates import generate_candidates
from geo_engine.model import PlantingItem, Utility, Zone
from geo_engine.norms import PlantingNorms
from geo_engine.placement import greedy_select
from geo_engine.species import load_species


def plan_items(
    plan_key: str,
    utilities: list[Utility],
    zones: list[Zone],
    territory: BaseGeometry,
    planting_types: list[str],
    score_fn: Callable,
    norms: PlantingNorms,
) -> list[PlantingItem]:
    """Generate the plantings for one plan.

    Each planting_type is generated independently from the same buildable_area,
    with no cross-type exclusion — a tree/shrub landing on a lawn's area is
    expected, not a bug: a tree standing in grass is the normal case (a cutout in
    pavement around a trunk is the rare exception, not something this models).
    Trimming the lawn around what actually got planted, if wanted, is a separate,
    user-driven editing feature, not something generation should enforce.

    Point placement (tree/shrub) is randomized, not gridded (see candidates.py),
    seeded from `plan_key` + planting_type via zlib.crc32 — stable across
    processes and runs, unlike Python's own str hash. That is what makes this a
    pure function of the plan's recipe: recomputing a collapsed plan must return
    the exact same layout, not a fresh random one.
    """
    items: list[PlantingItem] = []
    species = load_species()

    for planting_type in planting_types:
        exclusion = build_exclusion_zone(utilities, zones, planting_type, norms)
        margin = norms.territory_margin_for(planting_type)
        buildable = buildable_area(territory, exclusion, zones, territory_margin_m=margin)
        seed = zlib.crc32(f"{plan_key}:{planting_type}".encode())
        candidates = generate_candidates(buildable, exclusion, planting_type, norms, zoning_zones=zones, seed=seed)
        selected = greedy_select(candidates, score_fn, norms)

        species_pool = species.get(planting_type)
        if species_pool:
            # A seed distinct from the candidate-scatter one above — species
            # assignment shouldn't be even conceptually tied to placement
            # randomness. Same reproducibility requirement either way.
            species_rng = random.Random(zlib.crc32(f"{plan_key}:{planting_type}:species".encode()))
            for item in selected:
                item.species = species_rng.choice(species_pool)

        items.extend(selected)
    return items
