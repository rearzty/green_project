"""Import utilities/zones from GeoJSON or Shapefile via GeoPandas.

Like the DXF reader, the mapping from a feature's attribute value to our
internal object_type/zone_type vocabulary is data-source specific, so it's
passed in rather than guessed.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pandas as pd

from geo_engine.crs import ensure_metric_crs
from geo_engine.model import Utility, Zone

UTILITY_TYPES = {
    "heat_network",
    "water_pipe",
    "sewer",
    "gas_pipe",
    "cable_line",
    "power_line_corridor",
}


def read_vector_file(
    path: str | Path,
    type_field: str,
    type_map: dict[str, str] | None = None,
) -> tuple[list[Utility], list[Zone], str | None]:
    """Read any GeoPandas-supported vector file (.geojson, .json, .shp) and
    split features into Utility/Zone lists based on `type_field`'s value,
    translated through `type_map` (raw value -> our object_type/zone_type).
    Values already matching our vocabulary can be left untranslated.

    Also returns the detected CRS when the file turned out to carry
    geographic (lat/lon) coordinates -- e.g. a bare GeoJSON, which defaults
    to WGS84 per RFC 7946 unless it says otherwise. Geometry is reprojected
    to the matching UTM zone before anything downstream sees it, so the
    caller doesn't have to. Returns None when the file has no CRS or an
    already-metric one -- unchanged, as before.

    GeoPandas assumes EPSG:4326 for *any* GeoJSON with no `crs` member,
    whether or not the numbers actually look like degrees -- confirmed
    live on data/samples/real_moscow_chistye_prudy.geojson, which has no
    `crs` key but holds plain UTM-ish meter coordinates (easting ~414000),
    and still comes back `gdf.crs.is_geographic == True`. So `is_geographic`
    alone isn't the unambiguous signal geo_engine/crs.py's docstring assumes
    it is -- it only reflects declared/assumed metadata, not the actual
    values. Bounds are checked too: reproject only when they're also within
    real lon/lat range, since that's the one case a CRS can be told apart
    from plain numbers with certainty. Otherwise leave it alone -- same as
    an explicit-but-wrong source_crs, guessing wrong here would silently
    feed nonsense coordinates into every buffer/distance downstream.
    """
    gdf = gpd.read_file(path)
    type_map = type_map or {}

    detected_crs: str | None = None
    if gdf.crs is not None and gdf.crs.is_geographic and len(gdf) > 0:
        minx, miny, maxx, maxy = gdf.total_bounds
        if -180 <= minx and maxx <= 180 and -90 <= miny and maxy <= 90:
            gdf = ensure_metric_crs(gdf)
            detected_crs = str(gdf.crs)

    utilities: list[Utility] = []
    zones: list[Zone] = []

    for _, row in gdf.iterrows():
        # GeoPandas fills every feature's missing properties with NaN rather
        # than omitting the column (the DataFrame schema is the union of all
        # features' properties across the whole file) — treat NaN as "field
        # absent", not as the literal string "nan", and never let it leak
        # into `attrs` (NaN isn't valid JSON, so it would fail on insert).
        raw_value = row.get(type_field)
        raw_type = "unknown" if pd.isna(raw_value) else str(raw_value)
        object_type = type_map.get(raw_type, raw_type)
        geometry = row.geometry
        if geometry is None or geometry.is_empty:
            continue

        attrs = {k: v for k, v in row.items() if k not in (type_field, "geometry") and not pd.isna(v)}

        if object_type in UTILITY_TYPES:
            utilities.append(Utility(geometry=geometry, object_type=object_type, layer_source=str(path), attrs=attrs))
        else:
            zones.append(Zone(geometry=geometry, zone_type=object_type, attrs=attrs))

    return utilities, zones, detected_crs
