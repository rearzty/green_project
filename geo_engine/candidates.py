"""Turn a buildable area (territory minus exclusion buffers) into concrete
planting candidates: a point grid for trees/shrubs, whole sub-polygons for lawns.
"""

from __future__ import annotations

import numpy as np
import shapely
from shapely.geometry import LineString, Point
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from geo_engine.model import PlantingCandidate, PlantingType, Zone
from geo_engine.norms import PlantingNorms


def _iter_polygons(geom: BaseGeometry):
    if geom is None or geom.is_empty:
        return
    if geom.geom_type == "Polygon":
        yield geom
    elif geom.geom_type in ("MultiPolygon", "GeometryCollection"):
        for part in geom.geoms:
            yield from _iter_polygons(part)


class ZoningIndex:
    """A spatial index over just the `zoning`-typed zones, built once per
    generate_candidates() call rather than re-filtering/re-scanning the
    project's whole zones list for every candidate.

    `_zoning_at` used to loop `for zone in zoning_zones: if zone.zone_type
    == "zoning" ...` for every single candidate, and `zoning_zones` is the
    project's *entire* zones list, not pre-filtered -- fine on the modest
    zone counts this was written and tested against, but real Mosgeotrest
    deliveries carry hundreds of thousands of zones of every other type
    (existing_greenery, road, building, and a large "unknown" bucket -- no
    DXF layer map has ever produced zone_type="zoning" at all). Confirmed
    live: 331,140 zones x ~8,000 raw tree candidates on "1. Олимпийская
    деревня" took 282 seconds in generate_candidates alone, almost entirely
    this loop re-scanning the same 331K-item list per candidate and finding
    nothing every time. The fix is the same shape as
    ml_scoring.features.build_existing_greenery_index: filter once, index
    once, O(log n) lookup per candidate instead of O(zones).
    """

    def __init__(self, zoning_zones: list[Zone] | None):
        self._zones = [z for z in (zoning_zones or []) if z.zone_type == "zoning"]
        self._tree = STRtree([z.geometry for z in self._zones]) if self._zones else None

    def category_at(self, point_or_poly: BaseGeometry) -> str | None:
        if self._tree is None:
            return None
        # query() is a bbox pre-filter (fast, may over-match); intersects()
        # confirms the actual geometry -- the standard two-step STRtree
        # pattern (see also compliance.py's query_nearest usage).
        for idx in self._tree.query(point_or_poly):
            zone = self._zones[int(idx)]
            if zone.geometry.intersects(point_or_poly):
                return zone.attrs.get("zoning_category")
        return None


def _boundary_segments(geom: BaseGeometry) -> list[LineString]:
    """Every polygon part of `geom`, cut into individual 2-point edge
    segments -- vectorized per ring (one `shapely.linestrings()` call over
    an (N-1, 2, 2) coordinate array), not one `LineString(...)` per edge in
    a Python loop, since a real exclusion zone's rings can carry tens of
    thousands of vertices between them."""
    segments: list[LineString] = []
    for polygon in _iter_polygons(geom):
        for ring in (polygon.exterior, *polygon.interiors):
            coords = np.asarray(ring.coords)
            if len(coords) < 2:
                continue
            pairs = np.stack([coords[:-1], coords[1:]], axis=1)
            segments.extend(shapely.linestrings(pairs))
    return segments


class ExclusionIndex:
    """Spatial index over an exclusion zone's boundary EDGES (not its whole
    polygon parts), built once per exclusion_zone value rather than paying
    the same lookup cost fresh on every disc/row.

    Two separate costs stacked here, found in that order profiling the real
    "1. Олимпийская деревня" run:

    1. `shapely.distance(geom, exclusion_zone)` against a single complex
       MultiPolygon is NOT indexed internally, and `shapely.prepare()` does
       not accelerate it either (checked directly against this GEOS build)
       -- GEOS considers the whole multi-part geometry on every call
       regardless of how many points that call carries.
       `generate_point_candidates` gets away with this because it makes ONE
       batched call per buildable-area polygon, amortizing that fixed cost
       over tens of thousands of points at once; `patterns.py::fill_group()`
       does the opposite -- one small (~400-point) call PER CURTAIN -- and
       measured **~5.0s per call, ~1180s total** across 235 curtains, the
       dominant cost of a 1512s combined run.
    2. Indexing on the exclusion zone's 271 top-level polygon PARTS (one
       `STRtree.query_nearest()` per curtain instead of per-point) cut that
       to ~911ms/call -- real, but still the dominant cost. Each "part" is
       itself a `unary_union` of thousands of individually-buffered utility
       segments, so a single part can carry tens of thousands of vertices;
       GEOS still has to walk a whole part's ring to get the exact distance
       to it once the STRtree hands back which part is nearest by bounding
       box.

    Indexing individual boundary EDGES instead fixes both: each STRtree leaf
    is a plain 2-point segment, so its bounding box is tight (real spatial
    pruning) and computing distance to the one or few candidates the tree
    returns is O(1), not O(part's vertex count). Correct here specifically
    because every caller only ever measures distance FROM a point already
    known to be outside every exclusion part (sampled from `buildable_area`,
    which is `territory.difference(exclusion_zone)` by construction) --
    distance-to-polygon and distance-to-its-boundary coincide for a point
    outside the polygon. Do not reuse this for a point that might be
    *inside* an exclusion part; it would silently return the wrong (nonzero)
    answer instead of 0.
    """

    def __init__(self, exclusion_zone: BaseGeometry | None):
        self._parts = _boundary_segments(exclusion_zone) if exclusion_zone is not None else []
        self._tree = STRtree(self._parts) if self._parts else None

    def distance(self, geom: BaseGeometry) -> float:
        """Nearest-edge distance for one geometry."""
        if self._tree is None:
            return float("inf")
        idx = self._tree.nearest(geom)
        if idx is None:
            return float("inf")
        return float(geom.distance(self._parts[int(idx)]))

    def distances(self, geoms) -> list[float]:
        """Nearest-edge distance for a batch of geometries, one vectorized
        STRtree.query_nearest() call -- the same two-step pattern
        compliance.py's _neighbour_checks() already uses, not a Python loop
        of single .nearest() calls."""
        geoms = list(geoms)
        if self._tree is None or not geoms:
            return [float("inf")] * len(geoms)
        pairs, measured = self._tree.query_nearest(geoms, all_matches=False, return_distance=True)
        result = [float("inf")] * len(geoms)
        for position, geom_index in enumerate(pairs[0]):
            result[int(geom_index)] = float(measured[position])
        return result


class TooManyCandidatesError(ValueError):
    """Raised before the expensive scatter/select work starts, when a
    spacing/territory-size combination would sample an unsafe number of raw
    candidate points. `min_distance_m` is user-controllable down to a small
    floor (0.5 m tree / 0.3 m shrub, see backend/app/schemas/plan.py's
    GenerateRequest) -- on anything but a small territory, the floor drives
    the oversampled raw point count (see _OVERSAMPLE_FACTOR below) into the
    tens of millions, since it scales with 1/min_distance_m². Measured live
    in this session: a 300x300m territory at shrub's 0.3 m floor alone
    produced ~3.9M raw samples and was still running past 10 minutes and 5GB
    RAM when killed -- this didn't exist yet at the time. Message is shown to
    the user as-is."""


# Calibrated against that live measurement (see TooManyCandidatesError), not
# a precisely-derived performance ceiling -- same "predohranitel against
# pathological input" spirit as MAX_3D_ITEMS/MAX_VISIBLE_MARKERS on the
# frontend (plan3d.ts/MapView.tsx), not a claim about what's realistic.
# Comfortably above default-settings usage on a real ~1.5x1.5km territory
# (measured ~362K tree / ~1.0M shrub raw samples -- see CLAUDE.md's
# real-scale benchmarks), comfortably below the failing case above.
_MAX_RAW_SAMPLES_PER_POLYGON = 2_000_000

# Dart-throwing oversample: how many random raw points to draw per polygon,
# relative to what a regular grid at `spacing` would have produced over the
# same bounding box (see generate_point_candidates). Pure rejection sampling
# needs more raw darts than a grid to reach comparable final density, because
# some fraction always lands too close to an already-accepted neighbor and
# gets rejected later by greedy_select -- 4x was enough in practice (see
# CLAUDE.md) to keep counts in the same ballpark as the old grid at the same
# spacing; raise it if a real run comes back noticeably sparser than expected.
_OVERSAMPLE_FACTOR = 4


def generate_point_candidates(
    buildable_area: BaseGeometry,
    exclusion_zone: BaseGeometry,
    planting_type: PlantingType,
    norms: PlantingNorms,
    zoning_zones: list[Zone] | None = None,
    seed: int = 0,
) -> list[PlantingCandidate]:
    """Randomly scatter candidate points inside buildable_area (dart-throwing),
    for point-planted types (tree, shrub) -- not a regular grid. The actual
    minimum-spacing enforcement happens downstream in placement.greedy_select
    (it already buffers each candidate by canopy_radius_m and rejects
    anything overlapping an already-accepted one) -- feeding it a random
    scatter instead of grid points turns that existing rejection logic into a
    dart-throwing approximation of Poisson-disc/blue-noise sampling for free,
    without writing a real Bridson's-algorithm implementation: greedy_select
    was already fast and already tested at real scale, this only changes
    what candidates it sees. A plain grid looked mathematically regular on
    real data -- see CLAUDE.md.

    `seed` must be derived deterministically from something stable per plan
    (pipeline_service._compute_planting_rows uses plan_id) -- generate_plan is
    otherwise a pure function of its recipe (CLAUDE.md), and
    ensure_materialized() has to reproduce a collapsed plan's exact layout,
    not a fresh random one, when it recomputes it later.

    The containment/clearance checks run as vectorized shapely calls over the
    whole batch of points at once rather than one `Point(...).contains(...)`
    per point in a Python loop — a fine species spacing (e.g. shrub's 1m)
    over a real (not 100x80m synthetic) territory can put the raw batch in
    the millions of points, where a per-point Python-level loop is the
    bottleneck (measured: minutes, vs. a couple of seconds vectorized).
    """
    spacing = norms.spacing_for(planting_type).min_distance_m
    zoning_index = ZoningIndex(zoning_zones)
    candidates: list[PlantingCandidate] = []
    rng = np.random.default_rng(seed)

    for polygon in _iter_polygons(buildable_area):
        if polygon.area < norms.min_candidate_area_m2.get(planting_type, 0.0):
            continue
        minx, miny, maxx, maxy = polygon.bounds
        # Same raw point count a grid at this spacing would sample over this
        # bounding box (see the old np.arange-based version), times the
        # oversample factor -- keeps the cost model directly comparable to
        # the benchmarks already measured for the grid approach.
        n_x = int((maxx - minx) / spacing) + 1
        n_y = int((maxy - miny) / spacing) + 1
        n_samples = n_x * n_y * _OVERSAMPLE_FACTOR
        if n_samples <= 0:
            continue
        if n_samples > _MAX_RAW_SAMPLES_PER_POLYGON:
            raise TooManyCandidatesError(
                f"Интервал {spacing} м слишком мал для территории такого размера — потребовалось бы "
                "непомерно много кандидатов на посадку. Увеличьте интервал или уменьшите площадь генерации."
            )

        xs = rng.uniform(minx, maxx, size=n_samples)
        ys = rng.uniform(miny, maxy, size=n_samples)
        grid_points = shapely.points(xs, ys)
        inside_points = grid_points[shapely.contains(polygon, grid_points)]
        if inside_points.size == 0:
            continue

        if exclusion_zone is not None and not exclusion_zone.is_empty:
            clearances = shapely.distance(inside_points, exclusion_zone)
        else:
            clearances = np.full(inside_points.size, float("inf"))

        for point, clearance in zip(inside_points, clearances):
            candidates.append(
                PlantingCandidate(
                    geometry=point,
                    planting_type=planting_type,
                    clearance_m=float(clearance),
                    zoning=zoning_index.category_at(point),
                )
            )
    return candidates


def generate_area_candidates(
    buildable_area: BaseGeometry,
    exclusion_zone: BaseGeometry,
    planting_type: PlantingType,
    norms: PlantingNorms,
    zoning_zones: list[Zone] | None = None,
) -> list[PlantingCandidate]:
    """Each sub-polygon of buildable_area becomes one whole-area candidate,
    for area-planted types (lawn).

    Clearance is looked up through `ExclusionIndex`, not the naive
    `_clearance()`/`geom.distance(exclusion_zone)` this used to call per
    polygon -- the exact same unindexed-composite-geometry cost that
    `ExclusionIndex` was already built to fix for `fill_group()`'s curtains
    (see its docstring), just never wired in here. Confirmed live profiling
    lawn generation on a real street (4. Харьковская, 62,070 buffered
    setback objects): 324 raw `shapely.distance()` calls against the full
    composite exclusion zone cost **63.1s of 238.2s total** (26%) for only
    162 sub-polygons -- `buildable_area`'s own sub-polygons are guaranteed
    outside every exclusion part by construction (it's
    `territory.difference(exclusion_zone)`), exactly the precondition
    `ExclusionIndex.distance()`'s docstring requires. Batched `.distances()`
    (one vectorized STRtree.query_nearest() call), not `.distance()` in a
    loop, for the same reason `fill_group()`/`compliance.py` already batch
    it. Regression: tests/test_geo_engine/test_candidates.py::TestGenerateAreaCandidatesClearance.
    """
    zoning_index = ZoningIndex(zoning_zones)
    exclusion_index = ExclusionIndex(exclusion_zone)
    min_area = norms.min_candidate_area_m2.get(planting_type, 0.0)

    polygons = [p for p in _iter_polygons(buildable_area) if p.area >= min_area]
    clearances = exclusion_index.distances(polygons)

    return [
        PlantingCandidate(
            geometry=polygon,
            planting_type=planting_type,
            clearance_m=clearance,
            area_m2=polygon.area,
            zoning=zoning_index.category_at(polygon),
        )
        for polygon, clearance in zip(polygons, clearances)
    ]


def generate_candidates(
    buildable_area: BaseGeometry,
    exclusion_zone: BaseGeometry,
    planting_type: PlantingType,
    norms: PlantingNorms,
    zoning_zones: list[Zone] | None = None,
    seed: int = 0,
) -> list[PlantingCandidate]:
    if planting_type == "lawn":
        # Area candidates are whole sub-polygons, not a sampled grid/scatter --
        # nothing here to randomize, seed is meaningless for this branch.
        return generate_area_candidates(buildable_area, exclusion_zone, planting_type, norms, zoning_zones)
    return generate_point_candidates(buildable_area, exclusion_zone, planting_type, norms, zoning_zones, seed)
