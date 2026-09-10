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

    def score_fn(candidate):
        return candidate.clearance_m if candidate.clearance_m != float("inf") else 5.0, "test"

    for planting_type in ["tree", "shrub"]:
        exclusion = build_exclusion_zone(utilities, zones, planting_type, NORMS)
        buildable = buildable_area(territory, exclusion, zones)
        candidates = generate_candidates(buildable, exclusion, planting_type, NORMS, zoning_zones=zones)
        items = greedy_select(candidates, score_fn, NORMS)

        min_distance = NORMS.spacing_for(planting_type).min_distance_m
        for i, a in enumerate(items):
            for b in items[i + 1 :]:
                assert a.geometry.distance(b.geometry) >= min_distance - 1e-6
