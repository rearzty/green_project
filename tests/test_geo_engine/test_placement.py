"""greedy_select's canopy-buffer conflict check (geo_engine/placement.py).

Complements the module's own docstring history: quad_segs=8 -> 32 already
fixed the case where two candidates a few millimetres apart were wrongly
both accepted (a chordal gap large enough for a `4.9963m apart, canopy_radius
2.5m` pair to slip through). quad_segs=32 alone still leaves a residual,
uncompensated gap of its own -- live on 13. Харьковский проезд, after the
row/density fix (planner.py) let a single street's row grow into the
thousands of candidates instead of being capped: two row-placed trees of the
same species landed 4.999 m apart against a required 5.0 m, just past
compliance.py's own 1 mm tolerance, even though nothing here reached the
already-fixed quad_segs=8 magnitude of error.
"""

from __future__ import annotations

import math

from shapely.geometry import Point

from geo_engine.model import PlantingCandidate
from geo_engine.norms import load_norms
from geo_engine.placement import _CANOPY_BUFFER_QUAD_SEGS, greedy_select

NORMS = load_norms().with_spacing_override("tree", 5.0)  # canopy_radius_m = 2.5


def _constant_score_fn(candidates):
    return [(1.0, "test") for _ in candidates]


def test_canopy_buffer_inflation_is_greater_than_one():
    """A regression on the constant itself, cheaper than the geometry test
    below: an inflation factor of exactly 1.0 (i.e. accidentally deleted)
    would silently bring back the bug this file exists to catch."""
    from geo_engine.placement import _CANOPY_BUFFER_INFLATION

    assert _CANOPY_BUFFER_INFLATION > 1.0


def test_two_candidates_a_hair_under_the_nominal_spacing_are_not_both_accepted():
    """Two points 4.999 m apart (1 mm short of the 5.0 m nominal spacing for
    canopy_radius_m=2.5), placed along the direction that lands exactly on
    the *worst* point of a quad_segs=32 buffer's chordal approximation (the
    angular midpoint between two of its vertices, `pi/(4*quad_segs)` off an
    axis-aligned placement -- an axis-aligned pair happens to land ON a
    vertex instead, where the approximation has zero error, and would not
    exercise this bug at all). Without the inflation fix, both buffers
    report "not intersecting" and both candidates get accepted despite being
    genuinely too close; with it, the second is correctly rejected.
    """
    half_edge_angle = math.pi / (4 * _CANOPY_BUFFER_QUAD_SEGS)
    distance = 4.999
    dx = distance * math.cos(half_edge_angle)
    dy = distance * math.sin(half_edge_angle)

    candidates = [
        PlantingCandidate(geometry=Point(0, 0), planting_type="tree", clearance_m=10.0),
        PlantingCandidate(geometry=Point(dx, dy), planting_type="tree", clearance_m=10.0),
    ]

    selected = greedy_select(candidates, _constant_score_fn, NORMS)

    assert len(selected) == 1


def test_two_candidates_at_a_genuinely_legal_distance_are_both_accepted():
    """The inflation must not overcorrect into rejecting candidates that are
    actually far enough apart -- same worst-case angular alignment as above,
    but at 5.2 m, comfortably past the 5.0 m requirement."""
    half_edge_angle = math.pi / (4 * _CANOPY_BUFFER_QUAD_SEGS)
    distance = 5.2
    dx = distance * math.cos(half_edge_angle)
    dy = distance * math.sin(half_edge_angle)

    candidates = [
        PlantingCandidate(geometry=Point(0, 0), planting_type="tree", clearance_m=10.0),
        PlantingCandidate(geometry=Point(dx, dy), planting_type="tree", clearance_m=10.0),
    ]

    selected = greedy_select(candidates, _constant_score_fn, NORMS)

    assert len(selected) == 2


def test_a_row_at_exactly_the_nominal_pitch_keeps_every_point():
    """Ряд и живая изгородь ставят точки ровно с шагом, равным интервалу. Раздутые
    буферы крон перекрывались у таких соседей на волосок, и отбор брал каждую
    вторую точку — 200 из 400 при любом шаге и направлении (на реальной изгороди
    0,5 м — 349 из 577). Две точки сравниваются по расстоянию между центрами."""
    for pitch in (0.3, 0.5, 5.0, 8.0):
        norms = load_norms().with_spacing_override("shrub", pitch)
        for degrees in (0, 17, 45):
            a = math.radians(degrees)
            candidates = [
                PlantingCandidate(geometry=Point(1000 + i * pitch * math.cos(a), 2000 + i * pitch * math.sin(a)),
                                  planting_type="shrub", clearance_m=5.0)
                for i in range(200)
            ]
            assert len(greedy_select(candidates, _constant_score_fn, norms)) == 200, (pitch, degrees)


def test_a_point_inside_an_accepted_lawn_footprint_is_still_rejected():
    """Пара точка–многоугольник по-прежнему идёт через буфер кроны."""
    from shapely.geometry import box

    candidates = [
        PlantingCandidate(geometry=box(0, 0, 10, 10), planting_type="lawn", clearance_m=10.0),
        PlantingCandidate(geometry=Point(5, 5), planting_type="tree", clearance_m=10.0),
        PlantingCandidate(geometry=Point(30, 30), planting_type="tree", clearance_m=10.0),
    ]
    scores = {id(c): s for c, s in zip(candidates, (1.0, 0.9, 0.8))}

    selected = greedy_select(candidates, lambda cs: [(scores[id(c)], "") for c in cs], NORMS)

    assert [i.planting_type for i in selected] == ["lawn", "tree"]
    assert (selected[1].geometry.x, selected[1].geometry.y) == (30, 30)
