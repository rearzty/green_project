"""Finding the site outline among the zones a reader produced.

Lives in geo_engine rather than in the backend because it is pure geometry and
because the CLI needs it without dragging in SQLAlchemy. `pipeline_service` and
`edit_service` re-export it under the names they already used.
"""

from __future__ import annotations

from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from geo_engine.model import Zone

TERRITORY_ZONE_TYPE = "territory"
_AREAL = ("Polygon", "MultiPolygon")


class MissingTerritoryError(ValueError):
    """The input has no territory boundary zone -- nothing can be generated.

    Message is shown to the user as-is."""


def territory_polygon(zones: list[Zone]) -> BaseGeometry:
    """The site outline: every areal territory zone, unioned.

    Unions rather than returning the first match, and ignores zones that are not
    areal at all. Both matter on real drawings: the pilot street's work area is
    genuinely two polygons (the street splits around a block, 29739 + 16132 m²),
    so taking one would silently drop a third of the site; and the same layer in
    the main drawing carries leftover 2- and 5-vertex fragments that arrive as
    LineStrings, which would otherwise be returned as "the territory" and make
    every candidate fall outside it.
    """
    areas = [
        zone.geometry
        for zone in zones
        if zone.zone_type == TERRITORY_ZONE_TYPE
        and zone.geometry is not None
        and not zone.geometry.is_empty
        and zone.geometry.geom_type in _AREAL
    ]
    if areas:
        return areas[0] if len(areas) == 1 else unary_union(areas)
    raise MissingTerritoryError(
        "В загруженном файле нет границы участка (слой territory / BOUNDARY) — план построить нельзя."
    )
