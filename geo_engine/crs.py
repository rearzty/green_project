"""Coordinate reference system handling.

The exact CRS used by Mosgeotrest source data is unknown until the real
task data opens (2026-09-15) — likely MSK-77 (Moscow local system) or a UTM
zone. Rather than hardcoding one, every geometry operation in geo_engine
works in whatever *projected, metric* CRS the input already declares (or an
explicit override), and we only convert to WGS84 at the API boundary for
GeoJSON output (Leaflet expects EPSG:4326).

If a source file has no CRS at all (e.g. a bare DXF with no georeferencing),
callers must supply one explicitly — geo_engine refuses to silently assume
degrees are meters, since that would corrupt every buffer distance.
"""

from __future__ import annotations

import geopandas as gpd
from pyproj import CRS

WGS84 = CRS.from_epsg(4326)


class UnknownCRSError(ValueError):
    """Raised when a geometry/GeoDataFrame has no CRS and none was supplied."""


def ensure_metric_crs(gdf: gpd.GeoDataFrame, source_crs: str | int | CRS | None = None) -> gpd.GeoDataFrame:
    """Return a copy of gdf guaranteed to be in a projected (metric) CRS.

    - If gdf already has a CRS, it's used as-is (assumed to already be metric;
      geographic CRSs are auto-projected to a local UTM zone based on centroid).
    - If gdf has no CRS, `source_crs` must be provided.
    """
    if gdf.crs is None:
        if source_crs is None:
            raise UnknownCRSError(
                "GeoDataFrame has no CRS and none was supplied. "
                "Pass source_crs explicitly (e.g. 'EPSG:3856' or a known MSK-77 EPSG code)."
            )
        gdf = gdf.set_crs(source_crs)

    if gdf.crs.is_geographic:
        utm_crs = gdf.estimate_utm_crs()
        gdf = gdf.to_crs(utm_crs)

    return gdf


def to_wgs84(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Reproject to EPSG:4326 for GeoJSON output to the frontend map."""
    if gdf.crs is None:
        raise UnknownCRSError("Cannot reproject to WGS84: GeoDataFrame has no CRS.")
    return gdf.to_crs(WGS84)
