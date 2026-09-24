"""Build the union of setback buffers ("exclusion zone") that a planting of a
given type is not allowed to intersect, per geo_engine/config/planting_norms.yaml.
"""

from __future__ import annotations

from shapely import unary_union
from shapely.geometry.base import BaseGeometry

from geo_engine.model import PlantingType, Utility, Zone
from geo_engine.norms import PlantingNorms
from geo_engine.species import Species


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
    species: Species | None = None,
    crown_reference_diameter_m: float = 5.0,
) -> BaseGeometry:
    """Union of every utility/zone buffered by its required setback for
    `planting_type`. Zone types with a 0.0 setback (e.g. lawn vs. cables)
    still buffer by 0, which is a geometric no-op but keeps the logic uniform.

    `species` matters and must be the same one the plan will actually use.
    After the Moscow acts were read, the required setback stopped being a
    single table value: МГСН 1.02-02 п. 4.2.8 sets per-species distances from
    heat mains, 743-ПП табл. 3.6.1 прим. 3 pushes a wide crown 10 m off a
    building, and прим. 1 to both tables scales the table values with crown
    diameter. Leaving those out here while `compliance.explain_items` applies
    them produces a plan the tool's own checker rejects — measured live on a
    real street: with a wide-crown species, 80 of 121 placements came back as
    violations purely because the generator had used the unadjusted table.
    """
    buffered: list[BaseGeometry] = []

    def required(object_type: str) -> float:
        return norms.resolve_setback(
            object_type, planting_type, species, crown_reference_diameter_m
        ).required_m

    for utility in utilities:
        # Round join/cap: a utility is a line (possibly bent), and a rounded
        # clearance radius around a pipe/cable is physically the right shape.
        setback = required(utility.object_type)
        buffered.append(utility.geometry.buffer(setback, quad_segs=_BUFFER_QUAD_SEGS))

    for zone in zones:
        if zone.zone_type not in norms.setbacks_m:
            continue
        # Mitre join: a zone (building, road) is rectilinear, and offsetting
        # it should stay rectilinear too -- shapely's default round join
        # rounds every corner, which for e.g. a building turns a rectangular
        # setback strip into a blob-cornered shape (visibly wrong on the map)
        # and bloats the corner into ~30 extra vertices for no reason.
        setback = required(zone.zone_type)
        buffered.append(zone.geometry.buffer(setback, quad_segs=_BUFFER_QUAD_SEGS, join_style="mitre"))

    if not buffered:
        # No constraints at all -> empty exclusion zone (everything is candidate territory).
        return unary_union([])

    return unary_union(buffered)


# "sidewalk" joined this list once real Polygon geometry for it existed to
# subtract at all -- historically the layer was always a LineString (a
# pedestrian-path *edge*, same shape as road's kerb), so the hard-obstacle
# filter below (Polygon/MultiPolygon only) silently skipped it and only the
# setback buffer (0.7/0.5/0.0 m) applied. Live case, 4. Харьковская улица:
# "ДВ_ПП_ДО_ТипN_..." pavement-repair-scope layers (see layer_rules.py)
# include real sidewalk-surface polygons -- a candidate sitting 1m from one
# edge of a 2m+ wide sidewalk is still standing on paved sidewalk, not just
# too close to its edge, and the setback alone can't express that (same
# reasoning as road/building here). Adding it here is a pure no-op for any
# project where sidewalk is still only ever a line, like before.
HARD_OBSTACLE_ZONE_TYPES = ("building", "road", "existing_greenery", "sidewalk")


def buildable_area(
    territory: BaseGeometry, exclusion_zone: BaseGeometry, other_zones: list[Zone], territory_margin_m: float = 0.0
) -> BaseGeometry:
    """Territory minus the exclusion zone minus any hard obstacles (buildings,
    existing pavement/roads, already-planted greenery) that are never
    plantable regardless of setback — unlike the setback buffers, these are
    subtracted as their exact footprint, not grown by a distance. Otherwise
    a candidate could land directly inside e.g. an existing tree/shrub bed;
    `ml_scoring` layering `existing_greenery_gap_score` on top only ranks
    candidates that are already legal, it doesn't make them legal.

    `territory_margin_m` (planting_norms.yaml's territory_margin_m, per
    planting_type) erodes the territory's own outer boundary inward before
    anything else is subtracted — a genuinely different operation from every
    other setback here: those grow an *obstacle* outward and subtract the
    result, which can't express "keep back from the property line" (there's
    no obstacle geometry to grow, the boundary itself is the constraint).
    Without this, a candidate could land right at the edge of the plot --
    found live on real data, see CLAUDE.md.
    """
    if territory_margin_m > 0:
        territory = territory.buffer(-territory_margin_m)
    # Only polygonal geometry can subtract area in the first place -- a
    # building/road read as an unclosed LineString (real Мосгеотрест data,
    # see CLAUDE.md) or a stray Point already contributes nothing to a
    # difference against a polygon. Live crash on real data (17. Грузинская
    # М ул): thousands of such LineStrings/Points mixed into this same list
    # made `unary_union(hard_obstacles)` a heterogeneous GeometryCollection,
    # and GEOS's overlay engine cannot always compute a result dimension for
    # a mixed-dimension second operand of `difference()`
    # ("AssertionFailedException: ... determine overlay result geometry
    # dimension") -- dropping the zero-area geometries here is a no-op for
    # the result and removes the crash at the source, rather than papering
    # over it with a repair/retry on the union.
    hard_obstacles = [
        z.geometry
        for z in other_zones
        if z.zone_type in HARD_OBSTACLE_ZONE_TYPES and z.geometry.geom_type in ("Polygon", "MultiPolygon")
    ]
    if exclusion_zone is not None and not exclusion_zone.is_empty:
        result = territory.difference(exclusion_zone)
    else:
        result = territory
    if hard_obstacles:
        result = result.difference(unary_union(hard_obstacles))
    return result
