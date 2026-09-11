"""Generate synthetic test territories so the whole pipeline (import -> buffers
-> candidates -> scoring -> placement -> DXF export) can be exercised and
tested before real Mosgeotrest data opens on 2026-09-15.

Run directly to dump a few sample territories as GeoJSON into data/synthetic/:
    python -m scripts.generate_synthetic_data
"""

from __future__ import annotations

import json
from pathlib import Path

import ezdxf
import numpy as np
from pyproj import Transformer
from shapely.affinity import translate
from shapely.geometry import LineString, Polygon, box, mapping

from geo_engine.io.dxf_reader import DEFAULT_LAYER_MAP
from geo_engine.model import Utility, Zone

# Inverse of DEFAULT_LAYER_MAP (object_type/zone_type -> DXF layer name), so
# a DXF written here round-trips through geo_engine.io.dxf_reader.read_dxf()
# using the same default mapping a real upload would use.
_LAYER_BY_OBJECT_TYPE = {object_type: layer for layer, (_, object_type) in DEFAULT_LAYER_MAP.items()}

DATA_DIR = Path(__file__).parent.parent / "data" / "synthetic"

UTILITY_TYPES = ["heat_network", "water_pipe", "gas_pipe", "cable_line"]
ZONING_CATEGORIES = ["residential", "recreational", "public"]

# Purely cosmetic anchor so the synthetic block lands somewhere recognizable
# on the real OpenStreetMap basemap (near Chistye Prudy, central Moscow)
# instead of floating at (0, 0) in whatever CRS the map happens to render in
# — it is NOT real survey data, just a real-world *location* for a made-up
# block. Coordinates are still plain UTM-37N meters (EPSG:32637 covers all of
# Moscow), so every buffer/spacing distance in planting_norms.yaml is still
# correct real-world meters; only the origin moved.
ANCHOR_LONLAT = (37.6394, 55.7658)
ANCHOR_CRS_EPSG = "EPSG:32637"
_anchor_x, _anchor_y = Transformer.from_crs("EPSG:4326", ANCHOR_CRS_EPSG, always_xy=True).transform(*ANCHOR_LONLAT)


def generate_synthetic_territory(
    seed: int = 0,
    width_m: float = 100.0,
    height_m: float = 80.0,
    n_utilities: int = 4,
    n_buildings: int = 2,
) -> dict:
    """Returns {"territory": Polygon, "utilities": [Utility], "zones": [Zone]}
    in EPSG:32637 (UTM 37N) meters, anchored near a real Moscow location (see
    ANCHOR_LONLAT) purely so it renders somewhere sensible on the demo map —
    upload with source_crs="EPSG:32637" for that reprojection to apply.
    """
    rng = np.random.default_rng(seed)

    territory = box(0, 0, width_m, height_m)

    # Utility corridor: roughly-parallel lines bundled near the road edge
    # (y=0), the way heat/water/gas/cable actually get laid in a shared
    # trench along a street — not long diagonals scribbled across the whole
    # courtyard, which don't correspond to how utilities are ever routed.
    utilities: list[Utility] = []
    corridor_y0 = height_m * 0.10
    corridor_spacing = 2.5
    for i in range(n_utilities):
        y = corridor_y0 + i * corridor_spacing
        jitter = rng.uniform(-0.5, 0.5)
        line = LineString([(2, y), (width_m * 0.5, y + jitter), (width_m - 2, y)])
        object_type = UTILITY_TYPES[i % len(UTILITY_TYPES)]
        utilities.append(Utility(geometry=line, object_type=object_type, layer_source="synthetic"))

    zones: list[Zone] = []
    zones.append(Zone(geometry=territory, zone_type="territory"))

    # Buildings line the back edge (opposite the road), like a real
    # courtyard enclosed by a row of buildings on one side, rather than
    # floating at random positions in the middle of the open space.
    margin = 5.0
    cursor_x = margin
    buildings: list[Polygon] = []
    for _ in range(n_buildings):
        bw = rng.uniform(15, 25)
        bh = rng.uniform(10, 16)
        if cursor_x + bw > width_m - margin:
            break
        by = height_m - bh - margin
        building = box(cursor_x, by, cursor_x + bw, by + bh)
        buildings.append(building)
        zones.append(Zone(geometry=building, zone_type="building"))
        cursor_x += bw + rng.uniform(8, 16)  # gap to the next building

    road = box(0, 0, width_m, 6)
    zones.append(Zone(geometry=road, zone_type="road"))

    # A small existing flower bed tucked 2m in front of a building's wall
    # (the courtyard-facing side, not the outer wall against the territory
    # edge), the way real courtyard greenery usually sits near an entrance
    # -- falls back to a fixed spot if somehow no building fit (tiny width_m).
    if buildings:
        fb_minx, fb_miny, _, _ = buildings[0].bounds
    else:
        fb_minx, fb_miny = margin, height_m * 0.6
    greenery_top = max(fb_miny - 2, corridor_y0 + n_utilities * corridor_spacing + 4)
    greenery = box(fb_minx, greenery_top - 6, fb_minx + 8, greenery_top)
    zones.append(Zone(geometry=greenery, zone_type="existing_greenery"))

    # Split the territory into a couple of zoning polygons (left/right halves)
    # so ml_scoring.features has non-uniform zoning_suitability to work with.
    left = box(0, 0, width_m / 2, height_m)
    right = box(width_m / 2, 0, width_m, height_m)
    zones.append(Zone(geometry=left, zone_type="zoning", attrs={"zoning_category": ZONING_CATEGORIES[seed % 2]}))
    zones.append(Zone(geometry=right, zone_type="zoning", attrs={"zoning_category": ZONING_CATEGORIES[(seed + 1) % 2]}))

    territory = translate(territory, xoff=_anchor_x, yoff=_anchor_y)
    utilities = [
        Utility(geometry=translate(u.geometry, xoff=_anchor_x, yoff=_anchor_y), object_type=u.object_type, layer_source=u.layer_source)
        for u in utilities
    ]
    zones = [
        Zone(geometry=translate(z.geometry, xoff=_anchor_x, yoff=_anchor_y), zone_type=z.zone_type, attrs=z.attrs)
        for z in zones
    ]

    return {"territory": territory, "utilities": utilities, "zones": zones}


def _feature(geometry, **properties) -> dict:
    return {"type": "Feature", "geometry": mapping(geometry), "properties": properties}


def to_geojson(scene: dict) -> dict:
    """Every feature carries a single `object_type` property — utilities and
    zones share the same classification field, matching what
    geo_engine.io.shp_geojson_reader.read_vector_file expects (it tells them
    apart by checking whether the value is a known utility type). The
    territory itself is one of scene["zones"] (zone_type="territory"), not a
    separate feature, so it isn't exported twice.
    """
    features = []
    for utility in scene["utilities"]:
        features.append(_feature(utility.geometry, object_type=utility.object_type))
    for zone in scene["zones"]:
        features.append(_feature(zone.geometry, object_type=zone.zone_type, **zone.attrs))
    return {"type": "FeatureCollection", "features": features}


def to_dxf(scene: dict, path: Path) -> None:
    """Write a scene as a DXF whose layer names match DEFAULT_LAYER_MAP, so
    geo_engine.io.dxf_reader.read_dxf() (using its default mapping, same as a
    real upload with no custom layer_map) reads it back into the same
    Utility/Zone objects — including zone_type="territory" from the BOUNDARY
    layer. zoning_category (a Zone.attrs value) has no DXF representation and
    is dropped, same limitation a real DXF upload would have.
    """
    doc = ezdxf.new(setup=True)
    for layer_name in _LAYER_BY_OBJECT_TYPE.values():
        if layer_name not in doc.layers:
            doc.layers.add(name=layer_name)
    msp = doc.modelspace()

    for utility in scene["utilities"]:
        layer = _LAYER_BY_OBJECT_TYPE[utility.object_type]
        msp.add_lwpolyline(list(utility.geometry.coords), dxfattribs={"layer": layer})

    for zone in scene["zones"]:
        layer = _LAYER_BY_OBJECT_TYPE.get(zone.zone_type)
        if layer is None:
            continue
        msp.add_lwpolyline(list(zone.geometry.exterior.coords), close=True, dxfattribs={"layer": layer})

    doc.saveas(str(path))


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for seed in range(3):
        scene = generate_synthetic_territory(seed=seed)

        geojson_path = DATA_DIR / f"territory_{seed}.geojson"
        geojson_path.write_text(json.dumps(to_geojson(scene), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {geojson_path}")

        dxf_path = DATA_DIR / f"territory_{seed}.dxf"
        to_dxf(scene, dxf_path)
        print(f"wrote {dxf_path}")


if __name__ == "__main__":
    main()
