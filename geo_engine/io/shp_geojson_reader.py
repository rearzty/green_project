"""Import utilities/zones from GeoJSON or Shapefile via GeoPandas.

Like the DXF reader, the mapping from a feature's attribute value to our
internal object_type/zone_type vocabulary is data-source specific, so it's
passed in rather than guessed.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd

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
) -> tuple[list[Utility], list[Zone]]:
    """Read any GeoPandas-supported vector file (.geojson, .json, .shp) and
    split features into Utility/Zone lists based on `type_field`'s value,
    translated through `type_map` (raw value -> our object_type/zone_type).
    Values already matching our vocabulary can be left untranslated.
    """
    gdf = gpd.read_file(path)
    type_map = type_map or {}

    utilities: list[Utility] = []
    zones: list[Zone] = []

    for _, row in gdf.iterrows():
        raw_type = str(row.get(type_field, "unknown"))
        object_type = type_map.get(raw_type, raw_type)
        geometry = row.geometry
        if geometry is None or geometry.is_empty:
            continue

        attrs = {k: v for k, v in row.items() if k not in (type_field, "geometry")}

        if object_type in UTILITY_TYPES:
            utilities.append(Utility(geometry=geometry, object_type=object_type, layer_source=str(path), attrs=attrs))
        else:
            zones.append(Zone(geometry=geometry, zone_type=object_type, attrs=attrs))

    return utilities, zones
