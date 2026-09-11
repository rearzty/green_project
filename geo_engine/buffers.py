"""Build the union of setback buffers ("exclusion zone") that a planting of a
given type is not allowed to intersect, per geo_engine/config/planting_norms.yaml.
"""

from __future__ import annotations

from shapely import unary_union
from shapely.geometry.base import BaseGeometry

from geo_engine.model import PlantingType, Utility, Zone
from geo_engine.norms import PlantingNorms


# Shapely approximates a buffer's round caps/corners with a polygon of
# `quad_segs` segments per quarter-circle; the default (8) leaves a chordal
# gap of a couple millimeters at a setback of a few tenths of a meter (now
# that lawn's setbacks are 0.3-0.5m, not just tree/shrub's 1.5-5.0m, that gap
# is no longer negligible relative to the required distance). A higher
# resolution keeps the buffered boundary within ~0.1mm of the true circle at
# any setback distance we use.
_BUFFER_QUAD_SEGS = 32


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
        # Round join/cap: a utility is a line (possibly bent), and a rounded
        # clearance radius around a pipe/cable is physically the right shape.
        setback = norms.setback_for(utility.object_type, planting_type)
        buffered.append(utility.geometry.buffer(setback, quad_segs=_BUFFER_QUAD_SEGS))

    for zone in zones:
        if zone.zone_type not in norms.setbacks_m:
            continue
        # Mitre join: a zone (building, road) is rectilinear, and offsetting
        # it should stay rectilinear too -- shapely's default round join
        # rounds every corner, which for e.g. a building turns a rectangular
        # setback strip into a blob-cornered shape (visibly wrong on the map)
        # and bloats the corner into ~30 extra vertices for no reason.
        setback = norms.setback_for(zone.zone_type, planting_type)
        buffered.append(zone.geometry.buffer(setback, quad_segs=_BUFFER_QUAD_SEGS, join_style="mitre"))

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
