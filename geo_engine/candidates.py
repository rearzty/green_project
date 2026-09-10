"""Turn a buildable area (territory minus exclusion buffers) into concrete
planting candidates: a point grid for trees/shrubs, whole sub-polygons for lawns.
"""

from __future__ import annotations

import numpy as np
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry

from geo_engine.model import PlantingCandidate, PlantingType, Zone
from geo_engine.norms import PlantingNorms


def _iter_polygons(geom: BaseGeometry):
    if geom is None or geom.is_empty:
        return
    if geom.geom_type == "Polygon":
        yield geom
    elif geom.geom_type in ("MultiPolygon", "GeometryCollection"):
        for part in geom.geoms:
            yield from _iter_polygons(part)


def _zoning_at(point_or_poly: BaseGeometry, zoning_zones: list[Zone]) -> str | None:
    for zone in zoning_zones:
        if zone.zone_type == "zoning" and zone.geometry.intersects(point_or_poly):
            return zone.attrs.get("zoning_category")
    return None


def _clearance(geom: BaseGeometry, exclusion_zone: BaseGeometry | None) -> float:
    if exclusion_zone is None or exclusion_zone.is_empty:
        return float("inf")
    return geom.distance(exclusion_zone)


def generate_point_candidates(
    buildable_area: BaseGeometry,
    exclusion_zone: BaseGeometry,
    planting_type: PlantingType,
    norms: PlantingNorms,
    zoning_zones: list[Zone] | None = None,
) -> list[PlantingCandidate]:
    """Grid-sample points inside buildable_area, spaced by the species' minimum
    distance requirement, for point-planted types (tree, shrub)."""
    spacing = norms.spacing_for(planting_type).min_distance_m
    zoning_zones = zoning_zones or []
    candidates: list[PlantingCandidate] = []

    for polygon in _iter_polygons(buildable_area):
        if polygon.area < norms.min_candidate_area_m2.get(planting_type, 0.0):
            continue
        minx, miny, maxx, maxy = polygon.bounds
        xs = np.arange(minx, maxx + spacing, spacing)
        ys = np.arange(miny, maxy + spacing, spacing)
        for x in xs:
            for y in ys:
                point = Point(x, y)
                if not polygon.contains(point):
                    continue
                candidates.append(
                    PlantingCandidate(
                        geometry=point,
                        planting_type=planting_type,
                        clearance_m=_clearance(point, exclusion_zone),
                        zoning=_zoning_at(point, zoning_zones),
                    )
                )
    return candidates


def generate_area_candidates(
    buildable_area: BaseGeometry,
    exclusion_zone: BaseGeometry,
    planting_type: PlantingType,
    norms: PlantingNorms,
    zoning_zones: list[Zone] | None = None,
) -> list[PlantingCandidate]:
    """Each sub-polygon of buildable_area becomes one whole-area candidate,
    for area-planted types (lawn)."""
    zoning_zones = zoning_zones or []
    candidates: list[PlantingCandidate] = []
    min_area = norms.min_candidate_area_m2.get(planting_type, 0.0)

    for polygon in _iter_polygons(buildable_area):
        if polygon.area < min_area:
            continue
        candidates.append(
            PlantingCandidate(
                geometry=polygon,
                planting_type=planting_type,
                clearance_m=_clearance(polygon, exclusion_zone),
                area_m2=polygon.area,
                zoning=_zoning_at(polygon, zoning_zones),
            )
        )
    return candidates


def generate_candidates(
    buildable_area: BaseGeometry,
    exclusion_zone: BaseGeometry,
    planting_type: PlantingType,
    norms: PlantingNorms,
    zoning_zones: list[Zone] | None = None,
) -> list[PlantingCandidate]:
    if planting_type == "lawn":
        return generate_area_candidates(buildable_area, exclusion_zone, planting_type, norms, zoning_zones)
    return generate_point_candidates(buildable_area, exclusion_zone, planting_type, norms, zoning_zones)
