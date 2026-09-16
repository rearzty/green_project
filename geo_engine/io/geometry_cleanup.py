"""Cleanup for geometry that arrives from a real CAD export rather than from a
clean GIS source.

Everything here is a pure shapely transform — no ezdxf, no file I/O — so it can
be unit-tested on hand-built geometry and reused by any reader.

Motivated by the real Mosgeotrest geobase sheets in the pilot dataset
(`Пилотный проект 20 улиц`), where two properties of the export break naive
consumption of the geometry:

1. Utility lines are drawn with a dashed linetype, and the DWG->DXF export
   bakes that pattern into the geometry: a single pipe run arrives as hundreds
   of disconnected ~1 m segments separated by ~0.5 m gaps. Measured on
   `output_1-3__3_ДЖКХ-25_02794up.dwg`: 1265 gas segments, median segment
   length 1.00 m, median endpoint gap 0.50 m.
2. Legend/title-block symbols are inserted at the drawing origin, far from the
   surveyed area, and pollute any bbox or distance statistic computed over the
   raw extraction.

A third property, found while calibrating the stitching and deliberately NOT
"fixed" here: a pipe on a Russian topographic sheet is drawn as a run plus
periodic cross-ticks, and those ticks sit on the same layer as the pipe itself.
On the reference sheet, 709 of the pieces left after stitching are under 2 m,
69% of them meet their nearest long run at over 60 degrees, and they touch it
exactly — they are symbology, not pipe, and they carry 806 m of the layer's
3645 m. Two consequences worth knowing:

* Buffering them along with the pipe widens the exclusion corridor by roughly
  half a tick length (~0.5 m) at each tick. Small, and on the conservative side.
* Dropping them would be easy to write and is the wrong call: a short
  perpendicular stub off a main is also exactly what a service connection to a
  building looks like. Over-excluding by half a metre beats deleting a live gas
  service, so they stay in.

Note on (1): stitching dashes back together is a *data quality and
performance* fix, not a correctness prerequisite for the setback logic.
`buffers.build_exclusion_zone()` buffers utilities by 1.5-3 m, which is several
times the dash gap, so the buffered dashes already union into a continuous
corridor. What the stitching buys is (a) two orders of magnitude fewer
geometries to buffer/index, and (b) honest distance features for scoring —
distance to the nearest *dash* overestimates distance to the pipe whenever the
nearest point falls in a gap.
"""

from __future__ import annotations

import math

from shapely.geometry import LineString, MultiLineString, Point
from shapely.geometry.base import BaseGeometry
from shapely.ops import linemerge
from shapely.strtree import STRtree

# Widest gap between two dashes still treated as "same line". Measured gaps in
# the pilot geobase are 0.48-0.55 m; 0.8 m leaves headroom without being wide
# enough to bridge genuinely separate parallel runs (neighbouring cables in the
# same trench sit ~1 m apart there).
DEFAULT_DASH_GAP_M = 0.8

# How far two dashes may deviate from collinear and still be chained. A dashed
# line bends gently along the pipe run, so this cannot be near-zero; but it must
# stay well below 90 degrees or T-junctions between different runs get merged.
DEFAULT_ANGLE_TOLERANCE_DEG = 15.0

# Radius around the drawing origin (0, 0) inside which geometry is assumed to be
# a legend/title-block artifact rather than surveyed content.
DEFAULT_ORIGIN_RADIUS_M = 1.0

# How far outside the rest of the data the origin must sit — as a multiple of
# that data's own diagonal — before geometry on it counts as an artifact rather
# than as content. See drop_origin_artifacts for why this is relative and not a
# fixed distance.
DEFAULT_ORIGIN_EXTENT_RATIO = 2.0


def _flatten_lines(geometry: BaseGeometry | None) -> list[LineString]:
    if geometry is None or geometry.is_empty:
        return []
    if isinstance(geometry, LineString):
        return [geometry]
    if isinstance(geometry, MultiLineString):
        return [g for g in geometry.geoms if not g.is_empty]
    geoms = getattr(geometry, "geoms", None)
    if geoms is None:
        return []
    out: list[LineString] = []
    for g in geoms:
        out.extend(_flatten_lines(g))
    return out


def _unit(dx: float, dy: float) -> tuple[float, float] | None:
    length = math.hypot(dx, dy)
    if length == 0:
        return None
    return dx / length, dy / length


def _outward_direction(coords: list[tuple[float, float]], at_end: bool) -> tuple[float, float] | None:
    """Unit vector pointing away from the line at one of its two endpoints."""
    if len(coords) < 2:
        return None
    if at_end:
        (x1, y1), (x0, y0) = coords[-1], coords[-2]
    else:
        (x1, y1), (x0, y0) = coords[0], coords[1]
    return _unit(x1 - x0, y1 - y0)


def _is_continuation(
    coords_a: list[tuple[float, float]],
    end_a: bool,
    coords_b: list[tuple[float, float]],
    end_b: bool,
    cos_tolerance: float,
) -> bool:
    """True when line B looks like the next dash of the same dashed line as A.

    Three independent checks, all needed: the two dashes must be collinear with
    each other, and the bridge between them must run along that same direction.
    Collinearity alone would happily join two parallel pipes lying side by side;
    the bridge check alone would join a dash to a perpendicular run it happens
    to end near.
    """
    dir_a = _outward_direction(coords_a, end_a)
    dir_b = _outward_direction(coords_b, end_b)
    if dir_a is None or dir_b is None:
        return False

    # A continues outward; B must continue outward the opposite way, i.e. the
    # two outward directions are anti-parallel.
    if -(dir_a[0] * dir_b[0] + dir_a[1] * dir_b[1]) < cos_tolerance:
        return False

    point_a = coords_a[-1] if end_a else coords_a[0]
    point_b = coords_b[-1] if end_b else coords_b[0]
    bridge = _unit(point_b[0] - point_a[0], point_b[1] - point_a[1])
    if bridge is None:
        # Endpoints coincide: nothing to orient the bridge by, and the
        # anti-parallel check above already established collinearity.
        return True

    if dir_a[0] * bridge[0] + dir_a[1] * bridge[1] < cos_tolerance:
        return False
    return -(dir_b[0] * bridge[0] + dir_b[1] * bridge[1]) >= cos_tolerance


def merge_dashed_lines(
    lines: list[BaseGeometry],
    gap_tolerance_m: float = DEFAULT_DASH_GAP_M,
    angle_tolerance_deg: float = DEFAULT_ANGLE_TOLERANCE_DEG,
) -> list[LineString]:
    """Stitch dash segments of a dashed linetype back into connected lines.

    Deliberately NOT `linemerge(unary_union(lines))`: `unary_union` nodes the
    input, splitting every line at every crossing with another line, so on a
    utility sheet it produces *more* pieces than it started with (measured on
    the pilot data: 1265 gas segments -> 2527 pieces). This joins only
    endpoint-to-endpoint, across a gap, and only when the two pieces are
    collinear -- so crossings are left alone.

    Returns a list of LineStrings; input that is not line-like is ignored.
    """
    parts: list[list[tuple[float, float]]] = []
    for geometry in lines:
        for line in _flatten_lines(geometry):
            coords = [(float(x), float(y)) for x, y in line.coords]
            if len(coords) >= 2:
                parts.append(coords)
    if not parts:
        return []

    # Join what already touches before looking at gaps -- cheaper, and it means
    # the gap search below works on longer pieces with better-defined direction.
    merged = _flatten_lines(linemerge([LineString(c) for c in parts]))
    parts = [[(float(x), float(y)) for x, y in line.coords] for line in merged]
    parts = [c for c in parts if len(c) >= 2]
    if len(parts) < 2:
        return [LineString(c) for c in parts]

    endpoints: list[tuple[tuple[float, float], int, bool]] = []
    for index, coords in enumerate(parts):
        endpoints.append((coords[0], index, False))
        endpoints.append((coords[-1], index, True))

    tree = STRtree([Point(p) for p, _, _ in endpoints])
    cos_tolerance = math.cos(math.radians(angle_tolerance_deg))

    candidates: list[tuple[float, int, int]] = []
    for a, (point_a, part_a, end_a) in enumerate(endpoints):
        for b in tree.query(Point(point_a).buffer(gap_tolerance_m)):
            b = int(b)
            if b <= a:
                continue
            point_b, part_b, end_b = endpoints[b]
            if part_a == part_b:
                continue
            gap = math.dist(point_a, point_b)
            if gap > gap_tolerance_m:
                continue
            if not _is_continuation(parts[part_a], end_a, parts[part_b], end_b, cos_tolerance):
                continue
            candidates.append((gap, a, b))

    # Shortest gaps win: where a dash end has several plausible continuations
    # (a junction), the physically closest one is the right chain.
    candidates.sort()
    joined_at: dict[int, int] = {}
    for _, a, b in candidates:
        if a in joined_at or b in joined_at:
            continue
        joined_at[a] = b
        joined_at[b] = a

    return [LineString(chain) for chain in _walk_chains(parts, endpoints, joined_at)]


def _walk_chains(
    parts: list[list[tuple[float, float]]],
    endpoints: list[tuple[tuple[float, float], int, bool]],
    joined_at: dict[int, int],
) -> list[list[tuple[float, float]]]:
    """Follow the accepted endpoint joins to concatenate parts into chains."""

    def endpoint_index(part: int, at_end: bool) -> int:
        return part * 2 + (1 if at_end else 0)

    consumed = [False] * len(parts)
    chains: list[list[tuple[float, float]]] = []

    def build(start_part: int, start_at_end: bool) -> list[tuple[float, float]]:
        """Walk from one end of `start_part` through every joined neighbour."""
        chain: list[tuple[float, float]] = []
        part, at_end = start_part, start_at_end
        while True:
            consumed[part] = True
            coords = parts[part] if at_end else list(reversed(parts[part]))
            chain.extend(coords if not chain else coords[1:] if chain[-1] == coords[0] else coords)
            exit_index = endpoint_index(part, at_end)
            neighbour = joined_at.get(exit_index)
            if neighbour is None:
                return chain
            _, next_part, next_end = endpoints[neighbour]
            if consumed[next_part]:
                return chain
            # We entered the neighbour at `next_end`, so we leave by its other end.
            part, at_end = next_part, not next_end

    # Start from open ends first so chains come out whole rather than split in
    # the middle; anything still unconsumed after that is a closed loop.
    for part in range(len(parts)):
        if consumed[part]:
            continue
        head_open = endpoint_index(part, False) not in joined_at
        tail_open = endpoint_index(part, True) not in joined_at
        if head_open:
            chains.append(build(part, True))
        elif tail_open:
            chains.append(build(part, False))

    for part in range(len(parts)):
        if not consumed[part]:
            chains.append(build(part, True))

    return [c for c in chains if len(c) >= 2]


def drop_origin_artifacts(
    geometries: list[BaseGeometry],
    radius_m: float = DEFAULT_ORIGIN_RADIUS_M,
    min_extent_ratio: float = DEFAULT_ORIGIN_EXTENT_RATIO,
) -> list[BaseGeometry]:
    """Drop geometry sitting on the drawing origin — when it is clearly an outlier.

    Legend blocks, title-block symbols and unplaced template content are inserted
    at (0, 0) in the pilot drawings, hundreds of metres from the surveyed strip,
    and pollute any bbox or distance statistic computed over the raw extraction.

    The catch, found by a test rather than by the data: "at the origin" is only
    evidence of an artifact when the real content is nowhere near the origin. A
    drawing whose site genuinely starts at (0, 0) — synthetic fixtures, anything
    re-based to a local frame — would otherwise have its boundary silently
    deleted, which is exactly the class of bug this module exists to prevent.

    So the origin is treated as an artifact only when it sits far outside the
    extent of everything else, measured against that extent's own size: at least
    `min_extent_ratio` times the diagonal of the remaining data. On the pilot
    sheets the surveyed strip spans ~1073 m diagonally and sits ~14 km from the
    origin in Y — a ratio of about 13. On a drawing that legitimately starts at
    the origin the ratio is below 1, and nothing is dropped.
    """
    if not origin_is_artifact(geometries, radius_m, min_extent_ratio):
        return list(geometries)
    return [g for g in geometries if not is_origin_artifact(g, radius_m)]


def origin_is_artifact(
    geometries: list[BaseGeometry],
    radius_m: float = DEFAULT_ORIGIN_RADIUS_M,
    min_extent_ratio: float = DEFAULT_ORIGIN_EXTENT_RATIO,
) -> bool:
    """Does this drawing put junk on the origin, or is the origin real content?

    Separate from the filtering so a caller holding objects that merely *carry*
    geometry (Utility/Zone) can make the decision once over everything and then
    filter with `is_origin_artifact`, instead of comparing object identities
    against a returned list.
    """
    near_origin = any(is_origin_artifact(g, radius_m) for g in geometries)
    if not near_origin:
        return False

    rest = [g for g in geometries if not is_origin_artifact(g, radius_m)]
    if not rest:
        # Everything is at the origin: that is the data, not junk around it.
        return False

    xs_min, ys_min, xs_max, ys_max = _total_bounds(rest)
    diagonal = math.hypot(xs_max - xs_min, ys_max - ys_min)
    if diagonal == 0:
        return False

    # Shortest distance from the origin to the rest of the data's bounding box.
    dx = max(xs_min, 0.0, -xs_max)
    dy = max(ys_min, 0.0, -ys_max)
    return math.hypot(dx, dy) >= diagonal * min_extent_ratio


def is_origin_artifact(geometry: BaseGeometry, radius_m: float = DEFAULT_ORIGIN_RADIUS_M) -> bool:
    """Predicate behind drop_origin_artifacts, for callers filtering objects
    that merely *carry* a geometry rather than lists of bare geometries.

    On its own it only answers "is this on the origin" — the outlier judgement
    lives in drop_origin_artifacts, so a caller using this directly must already
    know the drawing has the artifact.
    """
    return geometry.is_empty or geometry.distance(Point(0.0, 0.0)) <= radius_m


def _total_bounds(geometries: list[BaseGeometry]) -> tuple[float, float, float, float]:
    bounds = [g.bounds for g in geometries if not g.is_empty]
    if not bounds:
        return (0.0, 0.0, 0.0, 0.0)
    return (
        min(b[0] for b in bounds),
        min(b[1] for b in bounds),
        max(b[2] for b in bounds),
        max(b[3] for b in bounds),
    )
