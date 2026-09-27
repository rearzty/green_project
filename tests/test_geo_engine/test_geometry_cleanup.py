"""Cleanup of CAD-export artifacts: dashed-linetype stitching and origin junk.

Numbers in the fixtures below mirror what the pilot Mosgeotrest sheets actually
contain — ~1 m dashes with ~0.5 m gaps — so a regression here means the real
geobase stops importing correctly, not just a synthetic case.
"""

import pytest
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import linemerge, unary_union

from geo_engine.io.geometry_cleanup import (
    DEFAULT_DANGLE_BUFFER_M,
    DEFAULT_DASH_GAP_M,
    drop_origin_artifacts,
    merge_dashed_lines,
    reconstruct_closed_footprints,
    reconstruct_closed_road_polygons,
)

DASH_M = 1.0
GAP_M = 0.5


def _dashed_line(start, direction, n_dashes, dash=DASH_M, gap=GAP_M):
    """n dashes laid end to end along `direction`, separated by `gap`."""
    (x0, y0), (dx, dy) = start, direction
    segments = []
    for i in range(n_dashes):
        offset = i * (dash + gap)
        segments.append(
            LineString(
                [
                    (x0 + dx * offset, y0 + dy * offset),
                    (x0 + dx * (offset + dash), y0 + dy * (offset + dash)),
                ]
            )
        )
    return segments


def test_dashes_of_one_line_merge_into_a_single_line():
    segments = _dashed_line((0, 0), (1, 0), n_dashes=10)

    merged = merge_dashed_lines(segments)

    assert len(merged) == 1
    # End-to-end span is preserved: 10 dashes + 9 gaps.
    assert merged[0].length == 10 * DASH_M + 9 * GAP_M


def test_parallel_runs_stay_separate():
    """Two cables in the same trench sit about a metre apart in the pilot data.
    Bridging dash gaps must never zip them together — collinearity is checked
    against the dash direction, not just endpoint proximity.
    """
    segments = _dashed_line((0, 0), (1, 0), n_dashes=6) + _dashed_line((0, 1.0), (1, 0), n_dashes=6)

    merged = merge_dashed_lines(segments)

    assert len(merged) == 2


def test_perpendicular_run_is_not_absorbed():
    """A branch leaving a main at right angles ends within gap distance of it;
    joining them would invent a pipe that turns 90 degrees mid-dash.
    """
    main = _dashed_line((0, 0), (1, 0), n_dashes=6)
    branch = _dashed_line((3.0, 0.3), (0, 1), n_dashes=4)

    merged = merge_dashed_lines(main + branch)

    assert len(merged) == 2


def test_gap_wider_than_tolerance_is_not_bridged():
    left = _dashed_line((0, 0), (1, 0), n_dashes=3)
    right = _dashed_line((20.0, 0), (1, 0), n_dashes=3)

    merged = merge_dashed_lines(left + right)

    assert len(merged) == 2


def test_merging_does_not_split_at_crossings_the_way_unary_union_does():
    """Regression for the obvious-looking implementation.

    `linemerge(unary_union(lines))` nodes its input, so every crossing splits
    both lines and the piece count goes *up*: measured on one real sheet, 1265
    gas segments became 2527 pieces. merge_dashed_lines must not do that.
    """
    horizontal = _dashed_line((0, 0), (1, 0), n_dashes=10)
    vertical = _dashed_line((5.2, -5.0), (0, 1), n_dashes=10)

    merged = merge_dashed_lines(horizontal + vertical)
    naive = unary_union(horizontal + vertical)
    naive_pieces = linemerge(naive)
    naive_count = len(naive_pieces.geoms) if hasattr(naive_pieces, "geoms") else 1

    assert len(merged) == 2
    assert naive_count > len(merged)


def test_merge_is_robust_to_dash_order():
    segments = _dashed_line((0, 0), (1, 0), n_dashes=8)
    shuffled = [segments[i] for i in (3, 0, 7, 5, 1, 6, 2, 4)]

    assert len(merge_dashed_lines(shuffled)) == 1


def test_default_gap_tolerance_covers_the_measured_pilot_gaps():
    """Measured endpoint gaps on the pilot geobase: 0.48-0.55 m."""
    assert DEFAULT_DASH_GAP_M > 0.55


class TestDuplicateDashes:
    """Live find, "6. Камчатская улица": a real `cable_line` layer where every
    dash exists as a byte-identical second copy (23 duplicate pairs found in
    one small neighborhood, including already-long runs, not just short
    dashes). A duplicate sits 0 m from its twin -- closer than the ~0.5 m gap
    to the true next dash -- and `_is_continuation()`'s collinearity check
    genuinely passes for a duplicate read in the opposite direction, so the
    old code chained onto the duplicate and back, producing a mathematically
    exact 180-degree reversal (`cos_angle == -1.000` measured live between
    consecutive output segments) instead of a straight line. Buffering that
    reversed spike left a real gap in the exclusion zone next to the actual
    cable: a shrub was placed 0.03-0.11 m from a power cable where 0.7 m is
    required (743-ПП / СП 42.13330.2016).
    """

    def _has_reversal(self, line: LineString) -> bool:
        coords = list(line.coords)
        for i in range(len(coords) - 2):
            (x0, y0), (x1, y1), (x2, y2) = coords[i], coords[i + 1], coords[i + 2]
            d1, d2 = (x1 - x0, y1 - y0), (x2 - x1, y2 - y1)
            len1, len2 = (d1[0] ** 2 + d1[1] ** 2) ** 0.5, (d2[0] ** 2 + d2[1] ** 2) ** 0.5
            if len1 < 1e-9 or len2 < 1e-9:
                continue
            cos_angle = (d1[0] * d2[0] + d1[1] * d2[1]) / (len1 * len2)
            if cos_angle < -0.999:
                return True
        return False

    def test_every_dash_duplicated_still_merges_cleanly_with_no_reversal(self):
        segments = _dashed_line((0, 0), (1, 0), n_dashes=6)
        doubled = segments + [LineString(s.coords) for s in segments]

        merged = merge_dashed_lines(doubled)

        assert len(merged) == 1
        assert merged[0].length == 6 * DASH_M + 5 * GAP_M
        assert not self._has_reversal(merged[0])

    def test_exact_duplicate_of_a_single_line_is_dropped_not_doubled(self):
        line = LineString([(0, 0), (1, 0)])
        duplicate = LineString([(0, 0), (1, 0)])

        merged = merge_dashed_lines([line, duplicate])

        assert len(merged) == 1
        assert merged[0].length == 1.0

    def test_reversed_duplicate_is_also_dropped(self):
        line = LineString([(0, 0), (1, 0)])
        reversed_duplicate = LineString([(1, 0), (0, 0)])

        merged = merge_dashed_lines([line, reversed_duplicate])

        assert len(merged) == 1

    def test_two_genuinely_sequential_dashes_are_not_mistaken_for_duplicates(self):
        """Two real, distinct dashes of an ordinary dashed line (not
        coordinate-identical to each other) must still stitch together
        normally -- deduplication must never fire on merely-collinear
        neighbors, only on true coordinate-for-coordinate copies."""
        first = LineString([(0, 0), (1, 0)])
        second = LineString([(1.5, 0), (2.5, 0)])

        merged = merge_dashed_lines([first, second])

        assert len(merged) == 1
        assert merged[0].length == pytest.approx(2.5)


def test_origin_artifacts_are_dropped_and_real_content_is_kept():
    legend = Point(0.0, 0.0).buffer(0.2)
    surveyed = LineString([(700.0, 14500.0), (720.0, 14500.0)])

    kept = drop_origin_artifacts([legend, surveyed])

    assert kept == [surveyed]


def test_origin_predicate_matches_the_list_filter():
    """drop_origin_artifacts and is_origin_artifact must not drift apart —
    read_dxf filters Utility/Zone objects with the predicate, while callers
    holding bare geometry use the list form.
    """
    from geo_engine.io.geometry_cleanup import is_origin_artifact

    legend = Point(0.0, 0.0).buffer(0.2)
    surveyed = LineString([(700.0, 14500.0), (720.0, 14500.0)])

    assert is_origin_artifact(legend) is True
    assert is_origin_artifact(surveyed) is False
    assert drop_origin_artifacts([legend, surveyed]) == [surveyed]


def test_origin_geometry_survives_when_the_site_genuinely_starts_at_zero():
    """Found by a CLI test, not by the data: "sits on the origin" only means
    "artifact" when the real content is nowhere near the origin. A drawing
    re-based to a local frame starts at (0, 0) legitimately, and deleting its
    boundary is exactly the silent data loss this module exists to prevent.
    """
    from geo_engine.io.geometry_cleanup import origin_is_artifact

    boundary = LineString([(0, 0), (120, 0), (120, 90), (0, 90), (0, 0)])
    utility = LineString([(0, 45), (120, 45)])

    assert origin_is_artifact([boundary, utility]) is False
    assert drop_origin_artifacts([boundary, utility]) == [boundary, utility]


def test_origin_geometry_is_dropped_when_the_survey_is_far_away():
    """The pilot signature: the surveyed strip spans about a kilometre and sits
    some 14 km from the origin, where the legend blocks are.
    """
    from geo_engine.io.geometry_cleanup import origin_is_artifact

    legend = Point(0.0, 0.0).buffer(0.2)
    surveyed = [
        LineString([(370.0, 14078.0), (880.0, 14078.0)]),
        LineString([(370.0, 15023.0), (880.0, 15023.0)]),
    ]

    assert origin_is_artifact([legend, *surveyed]) is True
    assert drop_origin_artifacts([legend, *surveyed]) == surveyed


def test_a_drawing_entirely_at_the_origin_keeps_everything():
    from geo_engine.io.geometry_cleanup import origin_is_artifact

    only_origin = [Point(0.0, 0.0).buffer(0.3)]

    assert origin_is_artifact(only_origin) is False
    assert drop_origin_artifacts(only_origin) == only_origin


class TestReconstructClosedFootprints:
    """The pilot dataset's real "Здания" layer: 0 of 409 LWPOLYLINE entities
    checked were closed (`is_closed`) — a building outline arrives as an open
    ring, sometimes with the missing closing edge as a separate LINE entity on
    the same layer. `buffers.buildable_area()` subtracts hard obstacles by
    exact-footprint difference, which is a silent no-op against a zero-area
    LineString — measured live, 6 922 m² of one real street's own computed
    buildable_area for trees fell inside what should have been building
    footprints. These tests exercise the reconstruction in isolation, on
    hand-built line soup, independent of any DXF/ezdxf machinery.
    """

    def test_a_line_that_traces_its_own_way_back_to_start_becomes_a_polygon(self):
        """The vertex list happens to already include the closing point (last
        coordinate repeats the first) -- topologically a closed ring even
        though nothing at the DXF entity level (no `is_closed` flag) marked
        it as one.
        """
        outline = LineString([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)])

        result = reconstruct_closed_footprints([outline])

        assert len(result) == 1
        assert result[0].geom_type == "Polygon"
        assert result[0].area == pytest.approx(100.0)

    def test_outline_split_across_a_polyline_and_a_separate_closing_line(self):
        """The exact real-data shape: a 3-sided LWPOLYLINE plus one LINE
        entity supplying the missing 4th edge -- neither is closed alone, but
        together they trace a full ring. No single entity's own geometry
        carries enough information to close itself; only the union does.
        """
        three_sides = LineString([(0, 0), (10, 0), (10, 10), (0, 10)])
        closing_edge = LineString([(0, 10), (0, 0)])

        result = reconstruct_closed_footprints([three_sides, closing_edge])

        assert len(result) == 1
        assert result[0].geom_type == "Polygon"
        assert result[0].area == pytest.approx(100.0)

    def test_a_genuinely_open_line_with_no_partner_is_not_turned_into_a_polygon(self):
        """The complement of the two tests above: three sides with nothing
        supplying the fourth (no closing entity anywhere in the input) is not
        a rectangle -- inventing the missing edge would be a guess this
        function has no basis for. It still becomes *some* obstacle (a thin
        buffered sliver along the drawn sides), just not a 100 m2 footprint.
        """
        three_sides_only = LineString([(0, 0), (10, 0), (10, 10), (0, 10)])

        result = reconstruct_closed_footprints([three_sides_only])

        assert len(result) == 1
        assert result[0].geom_type == "Polygon"
        assert result[0].area < 30.0  # a buffered 30 m open path, nowhere near a 100 m2 rectangle

    def test_two_disjoint_buildings_stay_two_separate_polygons(self):
        building_a = LineString([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)])
        building_b = LineString([(100, 100), (110, 100), (110, 110), (100, 110), (100, 100)])

        result = reconstruct_closed_footprints([building_a, building_b])

        assert len(result) == 2
        assert {round(p.area) for p in result} == {100, 100}

    def test_already_closed_polygons_pass_through_untouched(self):
        polygon = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])

        result = reconstruct_closed_footprints([polygon])

        assert result == [polygon]

    def test_a_genuinely_dangling_stub_is_buffered_not_dropped_and_not_shaped_into_a_polygon(self):
        """A short stub with no matching far end (drafting leftover, or a
        fragment this survey never completed) cannot be reconstructed into a
        real footprint -- but the old behaviour (a bare LineString, silently
        a no-op against buildable_area's difference()) is worse than a thin
        buffered sliver that at least still blocks a candidate.
        """
        dangling_stub = LineString([(50, 50), (55, 50)])

        result = reconstruct_closed_footprints([dangling_stub], dangle_buffer_m=0.3)

        assert len(result) == 1
        assert result[0].geom_type == "Polygon"
        # A buffered 5 m segment at 0.3 m radius is on the order of a few
        # square metres -- nowhere near what a closed rectangle of the same
        # bounding box would be, i.e. this really did stay a thin sliver.
        assert 0 < result[0].area < 10.0

    def test_mixed_input_keeps_polygons_and_reconstructs_lines_independently(self):
        already_closed = Polygon([(200, 200), (210, 200), (210, 210), (200, 210)])
        open_outline = LineString([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)])

        result = reconstruct_closed_footprints([already_closed, open_outline])

        assert len(result) == 2
        areas = sorted(round(p.area) for p in result)
        assert areas == [100, 100]

    def test_empty_input_is_a_no_op(self):
        assert reconstruct_closed_footprints([]) == []

    def test_default_dangle_buffer_is_small(self):
        """A regression on the constant itself: a large default would turn a
        genuinely unclosable fragment into an oversized fake obstacle."""
        assert DEFAULT_DANGLE_BUFFER_M <= 1.0

    def test_a_wider_snap_grid_closes_a_bigger_gap(self):
        """Live case, 12. Наташинский пр-д: a road-corridor work boundary
        (649 vertices) gaps by 22.9 cm at its own closing seam -- past
        DEFAULT_SNAP_GRID_M (5 cm, what "building" still uses), so this
        street's real geometry (not reproduced vertex-for-vertex here) needs
        a wider grid, which `read_dxf_bundle` supplies for
        `zone_type="territory"` via `RECONSTRUCT_FOOTPRINT_ZONE_TYPES`. A
        genuinely large gap (5 m, matching the "not a drafting slip" case
        used elsewhere in this suite) stays unclosed even at that wider
        grid -- the tolerance is generous, not unbounded.
        """
        ring = LineString([(0, 0), (100, 0), (100, 80), (50, 110), (0, 80), (0.23, 0)])
        genuinely_open = LineString([(0, 0), (100, 0), (100, 80), (0, 80), (0, 5)])

        wide_enough = reconstruct_closed_footprints([ring], snap_grid_m=0.3, dangle_buffer_m=0.0)
        still_too_far = reconstruct_closed_footprints([genuinely_open], snap_grid_m=0.3, dangle_buffer_m=0.0)

        assert len(wide_enough) == 1
        assert wide_enough[0].geom_type == "Polygon"
        assert wide_enough[0].area == pytest.approx(9488.5, abs=5.0)
        assert still_too_far == []

    def test_a_ring_that_self_touches_at_the_seam_splits_into_its_real_lobes(self):
        """Closing the gap is not the whole story: a long, hand-drafted
        boundary is more likely to touch itself by a hair right at its own
        seam than a simple rectangle is, which makes the naive closed
        `Polygon` invalid. `polygonize_full`'s noding resolves a self-touch
        correctly into separate simple rings (unlike patching an already
        -built invalid Polygon) -- this bowtie is a small, deterministic
        stand-in for that shape, not the pilot geometry itself.
        """
        ring = LineString([(0, 0), (10, 10), (10, 0), (0, 10), (0.01, 0.01)])

        result = reconstruct_closed_footprints([ring], snap_grid_m=0.3, dangle_buffer_m=0.0)

        assert len(result) == 2
        assert all(g.geom_type == "Polygon" for g in result)
        assert sum(g.area for g in result) == pytest.approx(49.0, abs=2.0)

    def test_zero_dangle_buffer_drops_unclosable_fragments_instead_of_faking_an_area(self):
        """Live case, 7. Нижние Поля ул: the only boundary-layer candidate in
        the whole bundle is an isolated 9.4 m stub with no partner anywhere
        to close it against. For a building, buffering that into a thin
        sliver is the right call (an imperfect obstacle beats a vanished
        one) -- for territory it is the wrong one: a fake ~6 m2 "work area"
        silently standing in for a street that has none in this input at all
        is worse than the loud MissingTerritoryError an absent boundary
        should raise. `dangle_buffer_m=0.0` (what
        RECONSTRUCT_FOOTPRINT_ZONE_TYPES sets for "territory") is how that is
        expressed: every unclosable leftover buffers to nothing.
        """
        stub = LineString([(15073.566, -1338.141), (15064.124, -1338.141)])

        with_fallback = reconstruct_closed_footprints([stub], snap_grid_m=0.3, dangle_buffer_m=0.3)
        without_fallback = reconstruct_closed_footprints([stub], snap_grid_m=0.3, dangle_buffer_m=0.0)

        assert with_fallback and with_fallback[0].area > 0
        assert without_fallback == []


class TestReconstructClosedRoadPolygons:
    """Live case, "2. Песчаный переулок": road/sidewalk are already in
    buffers.HARD_OBSTACLE_ZONE_TYPES, but that has been a no-op since either
    zone_type ever only had LineString geometry to offer the hard-obstacle
    filter (which only accepts Polygon/MultiPolygon). Unlike
    reconstruct_closed_footprints() above, this deliberately does NOT
    buffer unclosable leftovers into a sliver -- a curb network's open ends
    are usually the road legitimately continuing past the surveyed
    territory's edge, not a data gap, and the existing line-plus-setback
    path already covers that case correctly. Callers add these polygons
    alongside the original line zones, never in place of them.
    """

    def test_a_closed_curb_loop_becomes_a_road_polygon(self):
        loop = LineString([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)])

        result = reconstruct_closed_road_polygons([loop])

        assert len(result) == 1
        assert result[0].geom_type == "Polygon"
        assert result[0].area == pytest.approx(100.0)

    def test_a_loop_split_across_curb_segments_and_a_corner_arc_still_closes(self):
        """Stand-in for the real finding: a rectangular block's curb arrives
        as separate straight runs (as if from distinct kerb-code layers)
        plus what used to be an invisible corner -- now that ARC entities
        have geometry, the connecting piece is just another LineString here.
        """
        south = LineString([(0, 0), (10, 0)])
        east = LineString([(10, 0), (10, 10)])
        north = LineString([(10, 10), (0, 10)])
        west_and_corner = LineString([(0, 10), (0, 0)])

        result = reconstruct_closed_road_polygons([south, east, north, west_and_corner])

        assert len(result) == 1
        assert result[0].area == pytest.approx(100.0)

    def test_an_open_ended_curb_running_off_the_surveyed_edge_yields_no_polygon(self):
        """The road legitimately continues past this street's territory
        boundary -- there is no partner to close this line against, and
        unlike a building outline that should have closed, that is not a
        data defect to paper over with a buffered sliver."""
        dangling = LineString([(0, 0), (50, 0), (50, 3)])

        result = reconstruct_closed_road_polygons([dangling])

        assert result == []

    def test_a_closed_loop_and_a_separate_dangling_run_together(self):
        """The real mixed case: most of a street's curb network is one big
        open run, but a side loop (a courtyard entrance, a traffic island)
        closes on its own -- only the closed part should surface here."""
        loop = LineString([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)])
        dangling = LineString([(100, 100), (150, 100)])

        result = reconstruct_closed_road_polygons([loop, dangling])

        assert len(result) == 1
        assert result[0].area == pytest.approx(100.0)
