"""Import utilities/zones from a DXF drawing.

DXF has no notion of "this layer is a heat network" — that mapping is
project-specific and, for this hackathon, literally unknown until the real
Mosgeotrest drawings open on 2026-09-15. So the caller supplies a
`layer_map: dict[layer_name, (kind, object_type)]`; unmapped layers fall
back to `object_type="unknown"` so nothing is silently dropped.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import ezdxf
from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.base import BaseGeometry

from geo_engine.model import Utility, Zone

Kind = Literal["utility", "zone"]

# layer name -> (kind, object_type). object_type must match a key in
# planting_norms.yaml's setbacks_m for utilities, or a zone_type geo_engine
# understands ("building", "road", "zoning", "existing_greenery", "territory").
# "territory" is the one zone_type pipeline_service._territory_polygon()
# requires to find the overall site outline — must match across every
# reader (see shp_geojson_reader / generate_synthetic_data.py).
LayerMap = dict[str, tuple[Kind, str]]

DEFAULT_LAYER_MAP: LayerMap = {
    "HEAT_NETWORK": ("utility", "heat_network"),
    "WATER_PIPE": ("utility", "water_pipe"),
    "SEWER": ("utility", "sewer"),
    "GAS_PIPE": ("utility", "gas_pipe"),
    "CABLE_LINE": ("utility", "cable_line"),
    "POWER_LINE": ("utility", "power_line_corridor"),
    "BUILDING": ("zone", "building"),
    "ROAD": ("zone", "road"),
    "ZONING": ("zone", "zoning"),
    "EXISTING_GREENERY": ("zone", "existing_greenery"),
    "BOUNDARY": ("zone", "territory"),
}


def _entity_to_geometry(entity) -> BaseGeometry | None:
    dxftype = entity.dxftype()

    if dxftype == "LINE":
        start, end = entity.dxf.start, entity.dxf.end
        return LineString([(start.x, start.y), (end.x, end.y)])

    if dxftype in ("LWPOLYLINE", "POLYLINE"):
        points = [(p[0], p[1]) for p in entity.get_points()] if dxftype == "LWPOLYLINE" else [
            (v.dxf.location.x, v.dxf.location.y) for v in entity.vertices
        ]
        if len(points) < 2:
            return None
        is_closed = getattr(entity, "is_closed", False) or getattr(entity.dxf, "flags", 0) & 1
        if is_closed and len(points) >= 3:
            return Polygon(points)
        return LineString(points)

    if dxftype == "CIRCLE":
        center = entity.dxf.center
        return Point(center.x, center.y).buffer(entity.dxf.radius)

    if dxftype == "POINT":
        loc = entity.dxf.location
        return Point(loc.x, loc.y)

    return None


def read_dxf(path: str | Path, layer_map: LayerMap | None = None) -> tuple[list[Utility], list[Zone]]:
    """Parse a DXF file's modelspace into Utility and Zone lists, keyed by
    layer name via `layer_map` (defaults to DEFAULT_LAYER_MAP).
    """
    layer_map = layer_map or DEFAULT_LAYER_MAP
    doc = ezdxf.readfile(str(path))
    msp = doc.modelspace()

    utilities: list[Utility] = []
    zones: list[Zone] = []

    for entity in msp:
        geometry = _entity_to_geometry(entity)
        if geometry is None or geometry.is_empty:
            continue

        layer = entity.dxf.layer
        kind, object_type = layer_map.get(layer, ("zone", "unknown"))

        if kind == "utility":
            utilities.append(Utility(geometry=geometry, object_type=object_type, layer_source=layer))
        else:
            zones.append(Zone(geometry=geometry, zone_type=object_type, attrs={"layer": layer}))

    return utilities, zones
