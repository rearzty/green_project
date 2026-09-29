"""Tests for candidates.py's safety valve against pathologically dense
requests -- see TooManyCandidatesError's own docstring for the live incident
that motivated it (a 300x300m territory at shrub's minimum spacing, 0.3m,
was still running past 10 minutes and 5GB RAM when this didn't exist yet).
The check happens before any of the expensive sampling work, so these tests
stay fast even though they're exercising "would have been catastrophic" cases.
"""

import time

import numpy as np
import pytest
from shapely.geometry import MultiPolygon, Point, Polygon, box

from geo_engine.candidates import ExclusionIndex, TooManyCandidatesError, ZoningIndex, generate_point_candidates
from geo_engine.model import Zone
from geo_engine.norms import load_norms

NORMS = load_norms()


def test_minimum_shrub_spacing_on_a_modest_territory_is_rejected():
    # Reproduces this session's live incident at a smaller, deterministic
    # scale -- shrub_spacing_m at its own allowed floor (GenerateRequest's
    # ge=0.3, schemas/plan.py) on a 300x300m buildable area.
    territory = box(0, 0, 300, 300)
    norms = NORMS.with_spacing_override("shrub", 0.3)
    with pytest.raises(TooManyCandidatesError):
        generate_point_candidates(territory, None, "shrub", norms)


def test_minimum_tree_spacing_on_a_modest_territory_is_rejected():
    # Tree's own floor needs a bit more area than shrub's to cross the cap
    # (0.5m > 0.3m, so fewer raw samples per m² at the same territory size) --
    # 500x500m is still well inside "modest", nowhere near real-scale (1.5km).
    territory = box(0, 0, 500, 500)
    norms = NORMS.with_spacing_override("tree", 0.5)
    with pytest.raises(TooManyCandidatesError):
        generate_point_candidates(territory, None, "tree", norms)


def test_default_spacing_on_a_real_scale_territory_is_not_rejected():
    # ~1.5x1.5km, this project's own documented real-world scale,
    # at planting_norms.yaml's own defaults -- must stay well clear of the
    # cap, or the safety valve would be blocking the app's main use case.
    territory = box(0, 0, 1500, 1500)
    for planting_type in ("tree", "shrub"):
        candidates = generate_point_candidates(territory, None, planting_type, NORMS)
        assert len(candidates) > 0


def test_default_spacing_on_a_small_territory_is_not_rejected():
    territory = box(0, 0, 100, 80)
    for planting_type in ("tree", "shrub"):
        candidates = generate_point_candidates(territory, None, planting_type, NORMS)
        assert len(candidates) > 0


class TestZoningIndex:
    """Regression for a second live find on the same street as the
    existing_greenery one (ml_scoring/features.py): `_zoning_at()` looped
    `for zone in zoning_zones: if zone.zone_type == "zoning" ...` per
    candidate over the project's *entire*, unfiltered zones list. Real
    Mosgeotrest DXF data never produces zone_type="zoning" at all (no DXF
    layer map maps anything to it), so this scanned hundreds of thousands of
    irrelevant zones and found nothing, every single time. Confirmed live:
    331,140 zones x ~8,000 raw tree candidates on "1. Олимпийская деревня"
    -- 282 seconds in generate_candidates alone, for one planting type.
    """

    def test_finds_the_zoning_category_a_point_actually_falls_in(self):
        residential = Zone(
            geometry=Polygon([(0, 0), (0, 10), (10, 10), (10, 0)]),
            zone_type="zoning",
            attrs={"zoning_category": "residential"},
        )
        industrial = Zone(
            geometry=Polygon([(20, 0), (20, 10), (30, 10), (30, 0)]),
            zone_type="zoning",
            attrs={"zoning_category": "industrial"},
        )
        index = ZoningIndex([residential, industrial])

        assert index.category_at(Point(5, 5)) == "residential"
        assert index.category_at(Point(25, 5)) == "industrial"
        assert index.category_at(Point(50, 50)) is None

    def test_non_zoning_zones_are_ignored_not_mistaken_for_zoning(self):
        building = Zone(geometry=Polygon([(0, 0), (0, 10), (10, 10), (10, 0)]), zone_type="building", attrs={})
        index = ZoningIndex([building])

        assert index.category_at(Point(5, 5)) is None

    def test_empty_or_missing_zoning_zones_is_a_plain_none_not_an_error(self):
        assert ZoningIndex(None).category_at(Point(0, 0)) is None
        assert ZoningIndex([]).category_at(Point(0, 0)) is None

    def test_scanning_hundreds_of_thousands_of_non_zoning_zones_is_fast(self):
        """Not a synthetic worst case -- 331,140 is the real zone count from
        one pilot street, essentially none of it zone_type="zoning"."""
        zones = [
            Zone(geometry=Point(i % 500, i // 500).buffer(1.0), zone_type="existing_greenery", attrs={})
            for i in range(300_000)
        ]
        index = ZoningIndex(zones)
        points = [Point(i * 0.3, i * 0.2) for i in range(8_000)]

        started = time.perf_counter()
        for point in points:
            index.category_at(point)
        elapsed = time.perf_counter() - started

        # The naive per-candidate scan measured ~282s for a comparable count
        # on real data. 5s leaves generous headroom for a slower CI machine
        # without letting a real regression back to O(zones) slip through.
        assert elapsed < 5.0, f"scanning {len(points)} candidates took {elapsed:.1f}s -- looks like the O(n) path again"


class TestExclusionIndex:
    """Regression for `patterns.py::fill_group()` calling
    `shapely.distance(points, exclusion_zone)` fresh against the whole
    exclusion geometry once per curtain instead of once per planting type.

    Confirmed live on "1. Олимпийская деревня" (271-part exclusion zone,
    1.75M vertices total, 235 curtains): naive per-curtain
    `shapely.distance()` -- not sped up by `shapely.prepare()` either,
    checked directly -- measured ~5.0s/call, ~1180s total, the dominant cost
    of a 1512s combined (patterns + parallelism) run. Indexing on the
    exclusion zone's 271 top-level polygon *parts* (one `STRtree` leaf per
    part) only got that to ~911ms/call: each part is itself a `unary_union`
    of thousands of buffered utility segments, so GEOS still walks a whole
    part's ring once the tree hands back which part is nearest. Indexing on
    individual boundary EDGES instead (this class) dropped it to
    ~5ms/call -- each STRtree leaf is a plain 2-point segment, so its
    bounding box is tight and the exact-distance step is O(1). Combined
    real-data run: 1512s -> 380s, byte-identical item counts/species.
    """

    def test_distance_matches_the_naive_geom_distance(self):
        square = Polygon([(0, 0), (0, 10), (10, 10), (10, 0)])
        index = ExclusionIndex(square)
        point = Point(20, 5)
        assert index.distance(point) == pytest.approx(point.distance(square))

    def test_distances_batch_matches_per_point_naive_distance(self):
        square = Polygon([(0, 0), (0, 10), (10, 10), (10, 0)])
        index = ExclusionIndex(square)
        points = [Point(20, 5), Point(-5, 5), Point(5, 30)]
        naive = [p.distance(square) for p in points]
        indexed = index.distances(points)
        assert indexed == pytest.approx(naive)

    def test_multipolygon_input_finds_the_nearest_part(self):
        left = Polygon([(0, 0), (0, 10), (10, 10), (10, 0)])
        right = Polygon([(100, 0), (100, 10), (110, 10), (110, 0)])
        index = ExclusionIndex(MultiPolygon([left, right]))

        assert index.distance(Point(15, 5)) == pytest.approx(5.0)
        assert index.distance(Point(95, 5)) == pytest.approx(5.0)

    def test_empty_or_none_exclusion_is_an_infinite_distance(self):
        assert ExclusionIndex(None).distance(Point(0, 0)) == float("inf")
        assert ExclusionIndex(Polygon()).distance(Point(0, 0)) == float("inf")

    def test_matches_naive_distance_against_a_single_high_vertex_polygon(self):
        """The exact case that made the part-level index still slow: ONE
        polygon carrying thousands of vertices (a real exclusion zone part
        is a union of thousands of buffered utility segments, not a clean
        circle like this, but the vertex count and the failure mode --
        exact-distance-to-a-part being O(vertices) -- are the same)."""
        n = 4000
        angles = np.linspace(0, 2 * np.pi, n, endpoint=False)
        # tiny per-vertex jitter so it isn't a perfectly regular polygon
        # GEOS could special-case
        radii = 10 + (np.arange(n) % 3) * 1e-4
        coords = list(zip(radii * np.cos(angles), radii * np.sin(angles)))
        complex_poly = Polygon(coords)
        index = ExclusionIndex(complex_poly)
        probe = Point(50, 0)

        assert index.distance(probe) == pytest.approx(probe.distance(complex_poly), rel=1e-6)

    def test_indexed_batch_lookup_stays_fast_against_many_high_vertex_parts(self):
        """Not real-data scale (that's a live, opt-in verification) --
        enough vertices and queries
        that the naive or part-level-indexed path would visibly show up
        here too."""
        n = 2000
        parts = []
        for cx, cy in [(0, 0), (200, 0), (0, 200), (200, 200)]:
            angles = np.linspace(0, 2 * np.pi, n, endpoint=False)
            coords = list(zip(cx + 20 * np.cos(angles), cy + 20 * np.sin(angles)))
            parts.append(Polygon(coords))
        index = ExclusionIndex(MultiPolygon(parts))
        points = [Point(50 + i * 0.01, 50) for i in range(2000)]

        started = time.perf_counter()
        index.distances(points)
        elapsed = time.perf_counter() - started

        # The naive per-curtain call measured ~5.0s for a single batch of
        # 400 points against a 271-part/1.75M-vertex real exclusion zone;
        # 2s here is generous headroom on a much smaller synthetic case
        # while still catching a regression back to an unindexed path.
        assert elapsed < 2.0, f"distances() for {len(points)} points took {elapsed:.2f}s -- looks like the unindexed path again"


class TestGenerateAreaCandidatesClearance:
    """Regression for `generate_area_candidates()` (lawn's only candidate
    path) calling the naive `geom.distance(exclusion_zone)` per sub-polygon
    instead of going through `ExclusionIndex`, the same class of bug already
    fixed for `fill_group()`'s curtains (see TestExclusionIndex above) --
    just never wired in here. Confirmed live profiling a real street (4.
    Харьковская, 62,070 buffered setback objects): 63.1s of lawn's 238.2s
    total (26%) was 324 unindexed `shapely.distance()` calls against the
    full composite exclusion zone, for only 162 sub-polygons.
    """

    def test_clearance_matches_naive_distance_for_each_sub_polygon(self):
        from geo_engine.candidates import generate_area_candidates

        exclusion = Polygon([(0, 0), (0, 10), (10, 10), (10, 0)])
        # Two disjoint lawn-sized sub-polygons on either side of the
        # exclusion square, at different distances from it.
        near = Polygon([(15, 0), (15, 10), (25, 10), (25, 0)])
        far = Polygon([(-30, 0), (-30, 10), (-20, 10), (-20, 0)])
        buildable = MultiPolygon([near, far])

        candidates = generate_area_candidates(buildable, exclusion, "lawn", NORMS)

        assert len(candidates) == 2
        by_geometry = {c.geometry.wkt: c for c in candidates}
        assert by_geometry[near.wkt].clearance_m == pytest.approx(near.distance(exclusion))
        assert by_geometry[far.wkt].clearance_m == pytest.approx(far.distance(exclusion))

    def test_empty_exclusion_zone_gives_infinite_clearance(self):
        from geo_engine.candidates import generate_area_candidates

        buildable = Polygon([(0, 0), (0, 10), (10, 10), (10, 0)])
        candidates = generate_area_candidates(buildable, Polygon(), "lawn", NORMS)

        assert len(candidates) == 1
        assert candidates[0].clearance_m == float("inf")

    def test_sub_polygons_below_the_minimum_area_are_still_dropped(self):
        from geo_engine.candidates import generate_area_candidates

        tiny = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])  # 1 m^2, below lawn's 4.0 m^2 floor
        big = Polygon([(20, 0), (20, 10), (30, 10), (30, 0)])
        buildable = MultiPolygon([tiny, big])

        candidates = generate_area_candidates(buildable, Polygon(), "lawn", NORMS)

        assert len(candidates) == 1
        assert candidates[0].area_m2 == pytest.approx(100.0)
