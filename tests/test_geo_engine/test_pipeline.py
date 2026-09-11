from ml_scoring.heuristic_scorer import HeuristicScorer

from geo_engine.buffers import build_exclusion_zone, buildable_area
from geo_engine.candidates import generate_candidates
from geo_engine.norms import load_norms
from geo_engine.placement import greedy_select

NORMS = load_norms()
PLANTING_TYPES = ["tree", "shrub", "lawn"]


def test_buildable_area_is_subset_of_territory(synthetic_scene):
    territory, utilities, zones = synthetic_scene["territory"], synthetic_scene["utilities"], synthetic_scene["zones"]
    exclusion = build_exclusion_zone(utilities, zones, "tree", NORMS)
    buildable = buildable_area(territory, exclusion, zones)
    assert buildable.within(territory.buffer(1e-6))


def test_buildable_area_excludes_existing_greenery(synthetic_scene):
    """existing_greenery must be a hard obstacle (exact footprint subtracted),
    not just a soft ml_scoring distance feature — otherwise the placement
    algorithm can recommend planting directly inside already-planted areas.
    """
    territory, utilities, zones = synthetic_scene["territory"], synthetic_scene["utilities"], synthetic_scene["zones"]
    greenery = next(z.geometry for z in zones if z.zone_type == "existing_greenery")

    exclusion = build_exclusion_zone(utilities, zones, "tree", NORMS)
    buildable = buildable_area(territory, exclusion, zones)

    assert buildable.intersection(greenery).area < 1e-6


def test_exclusion_zone_grows_with_more_utilities(synthetic_scene):
    utilities, zones = synthetic_scene["utilities"], synthetic_scene["zones"]
    small = build_exclusion_zone(utilities[:1], [], "tree", NORMS)
    large = build_exclusion_zone(utilities, zones, "tree", NORMS)
    assert large.area >= small.area


def test_candidates_all_inside_buildable_area(synthetic_scene):
    territory, utilities, zones = synthetic_scene["territory"], synthetic_scene["utilities"], synthetic_scene["zones"]
    for planting_type in PLANTING_TYPES:
        exclusion = build_exclusion_zone(utilities, zones, planting_type, NORMS)
        buildable = buildable_area(territory, exclusion, zones)
        candidates = generate_candidates(buildable, exclusion, planting_type, NORMS, zoning_zones=zones)
        for candidate in candidates:
            assert buildable.buffer(1e-6).contains(candidate.geometry)


def test_greedy_select_respects_species_spacing(synthetic_scene):
    """No two selected same-type point plantings should end up closer than
    the species' minimum spacing requirement — that's the whole point of the
    canopy-footprint overlap check in geo_engine.placement.greedy_select.
    """
    territory, utilities, zones = synthetic_scene["territory"], synthetic_scene["utilities"], synthetic_scene["zones"]

    def score_fn(candidates):
        return [(c.clearance_m if c.clearance_m != float("inf") else 5.0, "test") for c in candidates]

    for planting_type in ["tree", "shrub"]:
        exclusion = build_exclusion_zone(utilities, zones, planting_type, NORMS)
        buildable = buildable_area(territory, exclusion, zones)
        candidates = generate_candidates(buildable, exclusion, planting_type, NORMS, zoning_zones=zones)
        items = greedy_select(candidates, score_fn, NORMS)

        min_distance = NORMS.spacing_for(planting_type).min_distance_m
        for i, a in enumerate(items):
            for b in items[i + 1 :]:
                assert a.geometry.distance(b.geometry) >= min_distance - 1e-6


# Buffers are polygons approximating circles (shapely quad_segs), so a
# buffered boundary is never *exactly* the nominal setback distance away —
# there's always some residual chordal error. buffers._BUFFER_QUAD_SEGS=32
# keeps that under ~0.1mm, which is what this tolerance reflects; it isn't
# slack for a real bug; 1e-6 was fine back when every setback was >=1.0m
# (tree/shrub) and the same sub-millimeter error was proportionally invisible.
_SETBACK_TOLERANCE_M = 1e-3


def test_selected_items_respect_every_setback_distance(synthetic_scene):
    """Every selected item must clear *its own object type's* required
    setback from *every* utility/zone that has one — not just "some buffer
    was applied somewhere". Originally written when `lawn`'s setbacks were
    all 0.0 (a draft planting_norms.yaml value, not a bug — confirmed by
    tree/shrub passing this same check) and later caught a real sub-mm
    buffer-precision regression once lawn got small (0.3-0.5m) real setbacks
    — see `_SETBACK_TOLERANCE_M`.
    """
    territory, utilities, zones = synthetic_scene["territory"], synthetic_scene["utilities"], synthetic_scene["zones"]
    scorer = HeuristicScorer(NORMS)

    for planting_type in PLANTING_TYPES:
        exclusion = build_exclusion_zone(utilities, zones, planting_type, NORMS)
        buildable = buildable_area(territory, exclusion, zones)
        candidates = generate_candidates(buildable, exclusion, planting_type, NORMS, zoning_zones=zones)
        items = greedy_select(candidates, scorer.as_score_fn(), NORMS)

        for item in items:
            for utility in utilities:
                required = NORMS.setback_for(utility.object_type, planting_type)
                assert item.geometry.distance(utility.geometry) >= required - _SETBACK_TOLERANCE_M
            for zone in zones:
                if zone.zone_type not in NORMS.setbacks_m:
                    continue
                required = NORMS.setback_for(zone.zone_type, planting_type)
                assert item.geometry.distance(zone.geometry) >= required - _SETBACK_TOLERANCE_M
