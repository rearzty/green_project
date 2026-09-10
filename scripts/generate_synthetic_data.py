"""Generate synthetic test territories so the whole pipeline (import -> buffers
-> candidates -> scoring -> placement -> DXF export) can be exercised and
tested before real Mosgeotrest data opens on 2026-09-15.

Run directly to dump a few sample territories as GeoJSON into data/synthetic/:
    python -m scripts.generate_synthetic_data
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from shapely.geometry import LineString, Polygon, box, mapping

from geo_engine.model import Utility, Zone

DATA_DIR = Path(__file__).parent.parent / "data" / "synthetic"

UTILITY_TYPES = ["heat_network", "water_pipe", "gas_pipe", "cable_line"]
ZONING_CATEGORIES = ["residential", "recreational", "public"]


def generate_synthetic_territory(
    seed: int = 0,
    width_m: float = 100.0,
    height_m: float = 80.0,
    n_utilities: int = 4,
    n_buildings: int = 2,
) -> dict:
    """Returns {"territory": Polygon, "utilities": [Utility], "zones": [Zone]}
    in a local metric CRS (coordinates are plain meters, origin at (0, 0)).
    """
    rng = np.random.default_rng(seed)

    territory = box(0, 0, width_m, height_m)

    utilities: list[Utility] = []
    for i in range(n_utilities):
        # A roughly straight utility line crossing the territory at a random angle/offset.
        y = rng.uniform(0.1, 0.9) * height_m
        jitter = rng.uniform(-5, 5)
        line = LineString([(0, y), (width_m * 0.5, y + jitter), (width_m, y - jitter)])
        object_type = UTILITY_TYPES[i % len(UTILITY_TYPES)]
        utilities.append(Utility(geometry=line, object_type=object_type, layer_source="synthetic"))

    zones: list[Zone] = []
    zones.append(Zone(geometry=territory, zone_type="territory"))

    for _ in range(n_buildings):
        bw, bh = rng.uniform(8, 20), rng.uniform(8, 20)
        bx = rng.uniform(5, width_m - bw - 5)
        by = rng.uniform(5, height_m - bh - 5)
        building = box(bx, by, bx + bw, by + bh)
        zones.append(Zone(geometry=building, zone_type="building"))

    road = box(0, height_m - 6, width_m, height_m)
    zones.append(Zone(geometry=road, zone_type="road"))

    greenery = box(width_m * 0.05, height_m * 0.05, width_m * 0.05 + 10, height_m * 0.05 + 10)
    zones.append(Zone(geometry=greenery, zone_type="existing_greenery"))

    # Split the territory into a couple of zoning polygons (left/right halves)
    # so ml_scoring.features has non-uniform zoning_suitability to work with.
    left = box(0, 0, width_m / 2, height_m)
    right = box(width_m / 2, 0, width_m, height_m)
    zones.append(Zone(geometry=left, zone_type="zoning", attrs={"zoning_category": ZONING_CATEGORIES[seed % 2]}))
    zones.append(Zone(geometry=right, zone_type="zoning", attrs={"zoning_category": ZONING_CATEGORIES[(seed + 1) % 2]}))

    return {"territory": territory, "utilities": utilities, "zones": zones}


def _feature(geometry, **properties) -> dict:
    return {"type": "Feature", "geometry": mapping(geometry), "properties": properties}


def to_geojson(scene: dict) -> dict:
    features = [_feature(scene["territory"], kind="territory")]
    for utility in scene["utilities"]:
        features.append(_feature(utility.geometry, kind="utility", object_type=utility.object_type))
    for zone in scene["zones"]:
        features.append(_feature(zone.geometry, kind="zone", zone_type=zone.zone_type, **zone.attrs))
    return {"type": "FeatureCollection", "features": features}


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for seed in range(3):
        scene = generate_synthetic_territory(seed=seed)
        out_path = DATA_DIR / f"territory_{seed}.geojson"
        out_path.write_text(json.dumps(to_geojson(scene), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
