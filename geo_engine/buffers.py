"""Build the union of setback buffers ("exclusion zone") that a planting of a
given type is not allowed to intersect, per geo_engine/config/planting_norms.yaml.
"""

from __future__ import annotations

from shapely import unary_union
from shapely.geometry.base import BaseGeometry

from geo_engine.model import PlantingType, Utility, Zone
from geo_engine.norms import PlantingNorms


def build_exclusion_zone(
    utilities: list[Utility],
    zones: list[Zone],
    planting_type: PlantingType,
    norms: PlantingNorms,
) -> BaseGeometry:
    """Union of every utility/zone buffered by its required setback for
    `planting_type`. Zone types with a 0.0 setback (e.g. lawn vs. cables)
    still buffer by 0, which is a geometric no-op but keeps the logic uniform.
    """
    buffered: list[BaseGeometry] = []

    for utility in utilities:
        setback = norms.setback_for(utility.object_type, planting_type)
        buffered.append(utility.geometry.buffer(setback))

    for zone in zones:
        if zone.zone_type not in norms.setbacks_m:
            continue
        setback = norms.setback_for(zone.zone_type, planting_type)
        buffered.append(zone.geometry.buffer(setback))

    if not buffered:
        # No constraints at all -> empty exclusion zone (everything is candidate territory).
        return unary_union([])

    return unary_union(buffered)


HARD_OBSTACLE_ZONE_TYPES = ("building", "road", "existing_greenery")


def buildable_area(territory: BaseGeometry, exclusion_zone: BaseGeometry, other_zones: list[Zone]) -> BaseGeometry:
    """Territory minus the exclusion zone minus any hard obstacles (buildings,
    existing pavement/roads, already-planted greenery) that are never
    plantable regardless of setback — unlike the setback buffers, these are
    subtracted as their exact footprint, not grown by a distance. Otherwise
    a candidate could land directly inside e.g. an existing tree/shrub bed;
    `ml_scoring` layering `existing_greenery_gap_score` on top only ranks
    candidates that are already legal, it doesn't make them legal.
    """
    hard_obstacles = [z.geometry for z in other_zones if z.zone_type in HARD_OBSTACLE_ZONE_TYPES]
    if exclusion_zone is not None and not exclusion_zone.is_empty:
        result = territory.difference(exclusion_zone)
    else:
        result = territory
    if hard_obstacles:
        result = result.difference(unary_union(hard_obstacles))
    return result
