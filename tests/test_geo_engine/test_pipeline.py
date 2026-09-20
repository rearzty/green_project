from shapely import unary_union
from shapely.geometry import LineString, Point, box

from ml_scoring.heuristic_scorer import HeuristicScorer

from geo_engine.buffers import build_exclusion_zone, buildable_area
from geo_engine.candidates import generate_candidates
from geo_engine.model import Zone
from geo_engine.norms import load_norms
from geo_engine.placement import greedy_select

NORMS = load_norms()
PLANTING_TYPES = ["tree", "shrub", "lawn"]

# Buffers are polygons approximating circles (shapely quad_segs), so a
# buffered boundary is never *exactly* the nominal setback/spacing distance
# away — there's always some residual chordal error. `placement.py`'s canopy
# buffer and `buffers._BUFFER_QUAD_SEGS` both use quad_segs=32, which keeps
# that under ~0.1mm; this tolerance reflects that floor, it isn't slack for a
# real bug. Random point placement (candidates.py's dart-throwing) can land
# candidates arbitrarily close to the exact spacing/setback threshold, unlike
# the old regular grid, which never produced a "just barely under" distance —
# that's what first surfaced how tight `1e-6` actually was.
_SETBACK_TOLERANCE_M = 1e-3


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


def test_buildable_area_ignores_non_polygonal_hard_obstacles():
    """Real Мосгеотрест data reads plenty of buildings/roads as bare
    LineString (an unclosed footprint outline) or even a stray Point -- see
    CLAUDE.md's "Здания на реальном чертеже -- не полигон". Either already
    contributes zero area to a difference against a polygon, but mixing them
    into `unary_union(hard_obstacles)` used to make that a heterogeneous
    GeometryCollection -- and GEOS's overlay engine cannot always compute a
    result dimension for that as `difference()`'s second operand. Live crash
    on real data (17. Грузинская М ул, thousands of such LineStrings/Points):
    "AssertionFailedException: ... determine overlay result geometry
    dimension". Only Polygon/MultiPolygon obstacles should reach the union;
    this must not crash, and must still subtract exactly the real building.
    """
    territory = box(0, 0, 10, 10)
    real_building = box(2, 2, 4, 4)
    unclosed_building_outline = LineString([(6, 6), (8, 6), (8, 8)])
    stray_vertex = Point(5, 5)
    zones = [
        Zone(geometry=real_building, zone_type="building"),
        Zone(geometry=unclosed_building_outline, zone_type="building"),
        Zone(geometry=stray_vertex, zone_type="building"),
    ]

    buildable = buildable_area(territory, unary_union([]), zones)

    assert buildable.intersection(real_building).area < 1e-9
    assert abs(buildable.area - (territory.area - real_building.area)) < 1e-9


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
                assert a.geometry.distance(b.geometry) >= min_distance - _SETBACK_TOLERANCE_M


def test_user_overridden_spacing_actually_changes_selected_distance(synthetic_scene):
    """End-to-end check for PlantingNorms.with_spacing_override (the
    per-generation interval control): plugging an overridden norms object
    into the same real candidates/greedy_select pipeline other tests use
    here must produce trees genuinely spaced by the new interval, not the
    YAML default -- exercising the whole path, not just the norms object in
    isolation (see tests/test_geo_engine/test_norms.py for that)."""
    territory, utilities, zones = synthetic_scene["territory"], synthetic_scene["utilities"], synthetic_scene["zones"]
    overridden = NORMS.with_spacing_override("tree", 10.0)
    assert overridden.spacing_for("tree").min_distance_m != NORMS.spacing_for("tree").min_distance_m

    def score_fn(candidates):
        return [(c.clearance_m if c.clearance_m != float("inf") else 5.0, "test") for c in candidates]

    exclusion = build_exclusion_zone(utilities, zones, "tree", overridden)
    buildable = buildable_area(territory, exclusion, zones)
    candidates = generate_candidates(buildable, exclusion, "tree", overridden, zoning_zones=zones)
    items = greedy_select(candidates, score_fn, overridden)

    assert len(items) > 1  # otherwise the spacing check below is vacuous
    for i, a in enumerate(items):
        for b in items[i + 1 :]:
            assert a.geometry.distance(b.geometry) >= 10.0 - _SETBACK_TOLERANCE_M


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


def test_territory_margin_keeps_plantings_off_the_property_line(synthetic_scene):
    """buffers.buildable_area()'s territory_margin_m erodes the territory
    boundary inward before generating candidates for that type -- without it,
    a candidate could land right at the edge of the plot (found live on real
    data by the user, see CLAUDE.md/decision_log.md). Every selected item
    must clear its own planting type's configured margin from the
    territory's own boundary line, not just from obstacles inside it.
    """
    territory, utilities, zones = synthetic_scene["territory"], synthetic_scene["utilities"], synthetic_scene["zones"]
    boundary = territory.boundary
    scorer = HeuristicScorer(NORMS)

    for planting_type in PLANTING_TYPES:
        exclusion = build_exclusion_zone(utilities, zones, planting_type, NORMS)
        margin = NORMS.territory_margin_for(planting_type)
        buildable = buildable_area(territory, exclusion, zones, territory_margin_m=margin)
        candidates = generate_candidates(buildable, exclusion, planting_type, NORMS, zoning_zones=zones)
        items = greedy_select(candidates, scorer.as_score_fn(), NORMS)

        assert items, f"expected at least one {planting_type} item to make this check non-vacuous"
        for item in items:
            assert item.geometry.distance(boundary) >= margin - _SETBACK_TOLERANCE_M


def test_generate_point_candidates_is_deterministic_for_a_given_seed(synthetic_scene):
    """generate_plan must stay a pure function of its recipe (CLAUDE.md) --
    ensure_materialized() has to reproduce a collapsed plan's exact layout
    later, not a fresh random one. Candidate generation is now randomized
    (dart-throwing, see candidates.py) instead of gridded, so this
    reproducibility guarantee is no longer automatic -- it depends entirely
    on the same seed producing the exact same raw candidates.
    """
    territory, utilities, zones = synthetic_scene["territory"], synthetic_scene["utilities"], synthetic_scene["zones"]
    exclusion = build_exclusion_zone(utilities, zones, "tree", NORMS)
    buildable = buildable_area(territory, exclusion, zones)

    first = generate_candidates(buildable, exclusion, "tree", NORMS, zoning_zones=zones, seed=12345)
    second = generate_candidates(buildable, exclusion, "tree", NORMS, zoning_zones=zones, seed=12345)

    assert len(first) == len(second) > 0
    assert [c.geometry.coords[:] for c in first] == [c.geometry.coords[:] for c in second]


def test_generate_point_candidates_differs_across_seeds(synthetic_scene):
    """Different seeds (a different plan_id/planting_type pair, see
    pipeline_service._compute_planting_rows) must give a genuinely different
    scatter -- otherwise the randomization isn't actually doing anything."""
    territory, utilities, zones = synthetic_scene["territory"], synthetic_scene["utilities"], synthetic_scene["zones"]
    exclusion = build_exclusion_zone(utilities, zones, "tree", NORMS)
    buildable = buildable_area(territory, exclusion, zones)

    a = generate_candidates(buildable, exclusion, "tree", NORMS, zoning_zones=zones, seed=1)
    b = generate_candidates(buildable, exclusion, "tree", NORMS, zoning_zones=zones, seed=2)

    assert [c.geometry.coords[:] for c in a] != [c.geometry.coords[:] for c in b]
