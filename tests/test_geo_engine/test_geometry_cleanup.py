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
