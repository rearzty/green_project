"""Turn a buildable area (territory minus exclusion buffers) into concrete
planting candidates: a point grid for trees/shrubs, whole sub-polygons for lawns.
"""

from __future__ import annotations

import numpy as np
import shapely
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry

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


def _zoning_at(point_or_poly: BaseGeometry, zoning_zones: list[Zone]) -> str | None:
    for zone in zoning_zones:
        if zone.zone_type == "zoning" and zone.geometry.intersects(point_or_poly):
            return zone.attrs.get("zoning_category")
    return None


def _clearance(geom: BaseGeometry, exclusion_zone: BaseGeometry | None) -> float:
    if exclusion_zone is None or exclusion_zone.is_empty:
        return float("inf")
    return geom.distance(exclusion_zone)


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
    zoning_zones = zoning_zones or []
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
                    zoning=_zoning_at(point, zoning_zones),
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
    for area-planted types (lawn)."""
    zoning_zones = zoning_zones or []
    candidates: list[PlantingCandidate] = []
    min_area = norms.min_candidate_area_m2.get(planting_type, 0.0)

    for polygon in _iter_polygons(buildable_area):
        if polygon.area < min_area:
            continue
        candidates.append(
            PlantingCandidate(
                geometry=polygon,
                planting_type=planting_type,
                clearance_m=_clearance(polygon, exclusion_zone),
                area_m2=polygon.area,
                zoning=_zoning_at(polygon, zoning_zones),
            )
        )
    return candidates


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
