"""Cleanup of CAD-export artifacts: dashed-linetype stitching and origin junk.

Numbers in the fixtures below mirror what the pilot Mosgeotrest sheets actually
contain — ~1 m dashes with ~0.5 m gaps — so a regression here means the real
geobase stops importing correctly, not just a synthetic case.
"""

from shapely.geometry import LineString, Point
from shapely.ops import linemerge, unary_union

from geo_engine.io.geometry_cleanup import (
    DEFAULT_DASH_GAP_M,
    drop_origin_artifacts,
    merge_dashed_lines,
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
