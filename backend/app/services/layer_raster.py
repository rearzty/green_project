"""Render a project's loaded source layers as a handful of PNGs instead of
vector features.

A real drawing's layers aren't interactive on the map — nothing here lets you
click, drag, or select a raw utility line or a building outline (only
`plan.features`, the generated planting, has that). Sending each one as its
own GeoJSON feature and letting react-leaflet mount a DOM node per object was
therefore pure cost with no payoff: on "1. Олимпийская деревня" (371,685
layer rows) it meant shipping the whole geometry over the wire and asking the
browser to lay out hundreds of thousands of SVG paths, which is what actually
produced the freeze the user hit live (VirtualizedLayers capped the visible
set, but the underlying render path — and the network payload before it —
was still fundamentally per-object). This also matches the brief's own
wording: the result belongs "on a separate layer *over* the original
drawing" — the drawing itself is a backdrop, not a set of live objects.

So instead: one raster per legend group (see mapStyle.ts::layerGroupKey —
mirrored here so a group here is the same group the panel's per-type toggle
already hides/shows), sharing one pixel grid so every group overlays the
exact same geographic rectangle. The frontend mounts one <ImageOverlay> per
visible group — a handful of DOM nodes regardless of whether the drawing has
a hundred objects or half a million.

Cheap to keep cached indefinitely: a project's layers are immutable after
upload (nothing in this codebase edits them), so a raster never goes stale
once built — the cache key is just the project id. Reads `project.layers`
directly now (a plain Python list already sitting in the in-process store,
see db/session.py) -- there used to be a separate lean-query path here
specifically to dodge an expensive SQLAlchemy ORM hydration on every
request, but there is no ORM and no query to avoid anymore, so that whole
concern is moot.
"""

from __future__ import annotations

import io
import os
import threading
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import Protocol

import numpy as np
import shapely
from fastapi.concurrency import run_in_threadpool
from PIL import Image, ImageDraw
from shapely.geometry.base import BaseGeometry

from backend.app.services.geo_io import _transformer_to_wgs84, db_to_shape, display_crs, layers_to_domain
from geo_engine.territory import MissingTerritoryError, territory_polygon


class _LayerLike(Protocol):
    """What render_layer_raster actually needs."""

    kind: str
    object_type: str
    attrs: dict | None
    geometry: object

# These are now the only source of truth for legend color/label — the
# frontend's own copy (mapStyle.ts's old ZONE_COLORS/LAYER_TYPE_LABELS) was
# deleted when vector rendering gave way to this server-rendered raster (see
# this module's docstring); ControlPanel.tsx just displays whatever `label`/
# `color` render_layer_raster() computed below, nothing to keep in sync
# against anymore.
_UTILITY_COLOR = "#b91c1c"
_ZONE_COLORS = {
    "building": "#78716c",
    "road": "#57534e",
    "territory": "#0ea5e9",
    # Deliberately teal, not the same green family as PLANTING_COLORS.tree
    # (#15803d) / .shrub (#65a30d) in mapStyle.ts. Was #16a34a -- close enough
    # to tree's #15803d that on a real street where existing_greenery is
    # mostly individual small tree-canopy polygons (thousands of small
    # polygons, not one big patch), it rendered as small green marks
    # indistinguishable from the plan's own newly-generated tree dots. Keep
    # in sync with mapStyle.ts's own ZONE_COLORS.existing_greenery (used by
    # ThreeDView's 3D extrusion) if this changes again.
    "existing_greenery": "#0d9488",
    # Заливки.dwg's asphalt/tile sidewalk fills (layer_rules.py's АБ ТР/ПЛ ТР
    # patterns) -- a lighter stone than road/building so it reads as related
    # hardscape without being mistaken for either.
    "sidewalk": "#a8a29e",
    # Existing lawn (Заливки.dwg's "гзн" surface-fill code) -- deliberately a
    # paler lime than existing_greenery's forest-green: this is real ground
    # cover, not a hard obstacle (see layer_rules.py), and shouldn't read as
    # the same "don't plant here" category on the map.
    "existing_lawn": "#bef264",
}
_ZONING_CATEGORY_COLORS = {
    "residential": "#a78bfa",
    "recreational": "#2dd4bf",
    "public": "#818cf8",
    "industrial": "#f97316",
    "transport": "#64748b",
}
_LAYER_TYPE_LABELS = {
    "territory": "Территория",
    "building": "Здания",
    "road": "Дороги",
    "existing_greenery": "Существующая зелень",
    "utility": "Инженерные сети",
    "sidewalk": "Тротуары",
    "existing_lawn": "Существующий газон",
}
_ZONING_CATEGORY_LABELS = {
    "residential": "Зонирование: жилая",
    "recreational": "Зонирование: рекреационная",
    "public": "Зонирование: общественная",
    "industrial": "Зонирование: промышленная",
    "transport": "Зонирование: транспортная",
}
_ZONING_PREFIX = "zoning:"

# Longer side of the rendered canvas, in pixels. Raised from 4000 once the
# map's own maxZoom went deeper (see MapView.tsx) than this backdrop could
# keep up with -- a fixed-resolution <ImageOverlay> doesn't gain real detail
# past its own pixel count, it just gets visibly blocky. 6000x6000 RGBA is
# ~144MB before PNG compression (~2x the old 4000px cap's ~64MB, since area
# scales with the square of the side) -- render cost per legend group scales
# the same way, which is exactly what the per-group process pool below
# (_PARALLEL_RENDER_THRESHOLD) exists to absorb.
_MAX_CANVAS_PX = 6000

# How far past the (clustered) territory boundary the canvas still extends,
# in metres -- generous enough to show the surrounding street/block context
# (adjacent buildings, the utilities a setback is actually measured against)
# without letting the frame balloon to fit whatever the file's largest stray
# fragment happens to be. Live case, 4. Харьковская улица: "unknown" alone
# has a fragment spanning tens of kilometres, and "utility"/"lighting_pole"
# each have one straggler thousands of metres past where every other
# category (building/road/existing_greenery/territory) agrees the real site
# is -- anchoring the canvas on the union of every layer let any single one
# of these silently set the frame for everything else, which is also why
# the whole picture read as "blurry": the same _MAX_CANVAS_PX pixel budget
# spread over an area dozens of times bigger than the real site.
_CONTEXT_MARGIN_M = 150.0
# "building" wider than the default: most of the real dataset's building
# outlines don't actually close into a polygon even after
# geometry_cleanup.reconstruct_closed_footprints()'s snap-and-polygonize pass
# (measured: 88% of real building zones stay a thin dangle-buffer sliver, not
# a true footprint -- see that function's docstring for why a more aggressive
# fix was tried and rejected as unsafe). A LineString at the default 2px reads
# as a stray scribble; at wall-like thickness it reads as what it is -- a real
# wall the survey drew, just one this reader couldn't close into a shape.
_LINE_WIDTH_PX = {"utility": 3, "building": 4}
_DEFAULT_LINE_WIDTH_PX = 2
_POINT_RADIUS_PX = 3
_FILL_ALPHA = 115  # ~0.45 opacity, matching MapView.tsx's layerStyle fillOpacity

# Each legend group's PNG is independent CPU-bound PIL work (own geometry
# list, own canvas) -- on "1. Олимпийская деревня" (371,685 rows) this loop
# was the measured 18-27s cold-render cost, single-threaded, one group after
# another. Below this many total geometries the whole thing is comfortably
# under a second already (any small synthetic demo territory), so spinning
# up worker processes would only add pure overhead -- Windows dev machines
# in particular pay a real per-process re-import cost that fork-based Linux
# containers don't. Gate the same way TooManyCandidatesError/
# MAX_VISIBLE_MARKERS gate their own expensive paths: on measured scale, not
# unconditionally. Worker count is capped by how many legend groups there
# actually are (usually well under ten: utility/building/road/territory/
# existing_greenery plus a handful of zoning categories) -- more workers
# than groups buys nothing, and one very large group (e.g. a pile of
# unrecognised "unknown" symbol-block primitives) still bounds the wall
# time of the whole render on its own, however many cores are free.
_PARALLEL_RENDER_THRESHOLD = 20_000
_MAX_RENDER_WORKERS = 6


def layer_group_key(layer: _LayerLike) -> str:
    """Same grouping as mapStyle.ts::layerGroupKey — utilities share one
    group regardless of medium, zoning splits by category, everything else
    groups by its own object_type."""
    if layer.kind == "utility":
        return "utility"
    if layer.object_type == "zoning":
        category = (layer.attrs or {}).get("zoning_category", "unknown")
        return f"{_ZONING_PREFIX}{category}"
    return layer.object_type


def _group_color(key: str) -> str:
    if key == "utility":
        return _UTILITY_COLOR
    if key.startswith(_ZONING_PREFIX):
        return _ZONING_CATEGORY_COLORS.get(key[len(_ZONING_PREFIX) :], "#9ca3af")
    return _ZONE_COLORS.get(key, "#9ca3af")


def _group_label(key: str) -> str:
    if key.startswith(_ZONING_PREFIX):
        category = key[len(_ZONING_PREFIX) :]
        return _ZONING_CATEGORY_LABELS.get(category, f"Зонирование: {category}")
    return _LAYER_TYPE_LABELS.get(key, key)


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


@dataclass
class LayerRasterGroup:
    key: str
    label: str
    color: str
    count: int
    png: bytes


@dataclass
class LayerRaster:
    # ((south, west), (north, east)) -- same shape react-leaflet's
    # <ImageOverlay bounds=.../> expects directly. WGS84 degrees when the
    # project's CRS is verified; otherwise raw local-unit numbers (see
    # geo_io.py::display_crs), which the frontend renders under Leaflet's
    # CRS.Simple instead of the real-world WGS84/Mercator CRS. None for an
    # empty project.
    bounds: tuple[tuple[float, float], tuple[float, float]] | None
    groups: list[LayerRasterGroup]


def _bounds_of(geoms: list[BaseGeometry]) -> tuple[float, float, float, float]:
    arr = shapely.bounds(np.array(geoms, dtype=object))
    return float(arr[:, 0].min()), float(arr[:, 1].min()), float(arr[:, 2].max()), float(arr[:, 3].max())


# Scales a median absolute deviation to be comparable to a normal
# distribution's standard deviation -- the standard MAD->sigma constant.
_MAD_TO_SIGMA = 1.4826
# How many (scaled) MADs from the median centroid a geometry may sit before
# _dominant_cluster_bounds treats it as "not really part of this site".
# Calibrated live on "7. Нижние Поля ул" (no usable territory boundary at
# all, see MissingTerritoryError -- this function's caller only runs this
# path when there's no territory to anchor on instead): the real site's
# utilities/road/building/etc. cluster tightly (median-centred spread ~400-
# 500m), but "unknown" alone carries a genuine second population of 26 605
# objects (not a single stray point) 2-3km further out -- almost certainly
# paperspace/inset content from the same main drawing, not the work site --
# plus a handful of scattered singletons (existing_greenery/building/
# lighting_pole) thousands of metres off in unrelated directions, none of
# them near the origin (drop_origin only catches artifacts AT the origin,
# not just anywhere far from the real site). Swept k=3..20 against this
# exact dataset: k<=5 cleanly excludes both (kept 99.9% of all objects, x/y
# range barely wider than the utility-only extent); k>=8 pulls the far
# cluster back in and the canvas balloons to the full, useless ~9x13km span
# again. 5 sits with a clear margin on the safe side of that cliff, not
# tuned to the exact boundary.
_ROBUST_OUTLIER_MADS = 5.0
# Floor under the MAD itself so a real, tightly-clustered site (or a small
# synthetic test fixture) doesn't get its own genuine, tiny spread treated
# as "zero tolerance for anything else" -- comparable order of magnitude to
# _CONTEXT_MARGIN_M below, not a load-bearing precise value.
_MIN_ROBUST_MAD_M = 50.0


def _dominant_cluster_bounds(geoms: list[BaseGeometry]) -> tuple[float, float, float, float]:
    """Same 'the real site is wherever most of the data actually is, not
    wherever the outer edges of any one layer happen to reach' idea as
    geo_engine.territory._dominant_cluster, generalized from "territory
    polygons only" to an arbitrary mix of layer types -- see this module's
    only caller, render_layer_raster's no-territory fallback, and
    _ROBUST_OUTLIER_MADS's own comment for the live case this was measured
    against. Median (not mean) and median-absolute-deviation (not standard
    deviation) throughout specifically because both are robust to the exact
    failure mode here -- a mean/stddev computed over a real cluster plus a
    26 605-object second population would itself be dragged toward the
    wrong answer, defeating the whole point.
    """
    if len(geoms) == 1:
        return geoms[0].bounds
    centroids = shapely.centroid(np.array(geoms, dtype=object))
    xs, ys = shapely.get_x(centroids), shapely.get_y(centroids)
    median_x, median_y = float(np.median(xs)), float(np.median(ys))
    mad_x = max(float(np.median(np.abs(xs - median_x))) * _MAD_TO_SIGMA, _MIN_ROBUST_MAD_M)
    mad_y = max(float(np.median(np.abs(ys - median_y))) * _MAD_TO_SIGMA, _MIN_ROBUST_MAD_M)
    keep = (np.abs(xs - median_x) <= mad_x * _ROBUST_OUTLIER_MADS) & (np.abs(ys - median_y) <= mad_y * _ROBUST_OUTLIER_MADS)
    kept = [g for g, k in zip(geoms, keep) if k]
    return _bounds_of(kept if kept else geoms)


def _wgs84_bounds(
    minx: float, miny: float, maxx: float, maxy: float, source_crs: str | None
) -> tuple[tuple[float, float], tuple[float, float]]:
    if not source_crs:
        # Same passthrough every other reprojection in geo_io.py falls back
        # to when source_crs is unset -- no known CRS, so the raw numbers
        # are handed to Leaflet as-is rather than guessed at.
        return (miny, minx), (maxy, maxx)
    transformer = _transformer_to_wgs84(source_crs)
    lon1, lat1 = transformer.transform(minx, miny)
    lon2, lat2 = transformer.transform(maxx, maxy)
    south, north = sorted((lat1, lat2))
    west, east = sorted((lon1, lon2))
    return (south, west), (north, east)


def _make_pixel_transform(minx: float, miny: float, maxx: float, maxy: float, max_dim: int):
    width_m = max(maxx - minx, 1e-6)
    height_m = max(maxy - miny, 1e-6)
    scale = max_dim / max(width_m, height_m)
    px_w = max(1, round(width_m * scale))
    px_h = max(1, round(height_m * scale))

    def to_px(x: float, y: float) -> tuple[float, float]:
        # Geo Y grows up, image Y grows down -- flip against the top (maxy).
        return (x - minx) * scale, (maxy - y) * scale

    # scale is returned alongside the closure, not just baked into it: a
    # worker process rendering one group in parallel (see
    # _render_group_worker) can't be handed `to_px` itself -- closures don't
    # pickle across a process boundary -- so it rebuilds the identical
    # transform from these three plain numbers instead.
    return to_px, px_w, px_h, scale


def _draw_geometry(draw: ImageDraw.ImageDraw, geom: BaseGeometry, to_px, color: tuple[int, int, int], group_key: str) -> None:
    gtype = geom.geom_type
    if gtype == "Point":
        x, y = to_px(geom.x, geom.y)
        r = _POINT_RADIUS_PX
        draw.ellipse([x - r, y - r, x + r, y + r], fill=color)
    elif gtype in ("LineString", "LinearRing"):
        points = [to_px(x, y) for x, y in geom.coords]
        if len(points) >= 2:
            width = _LINE_WIDTH_PX.get(group_key, _DEFAULT_LINE_WIDTH_PX)
            draw.line(points, fill=color, width=width)
    elif gtype == "Polygon":
        points = [to_px(x, y) for x, y in geom.exterior.coords]
        if len(points) >= 3:
            draw.polygon(points, fill=(*color, _FILL_ALPHA), outline=color)
        # Interior rings ARE punched out -- live case, «1. Олимпийская
        # деревня»: territory_polygon() now genuinely returns a Polygon
        # with 8 real holes (courtyards excluded from the work scope, see
        # geo_engine/territory.py::_combine_with_holes), not the solid blob
        # this used to assume was the common case. Drawing on an RGBA
        # canvas, fill=(0,0,0,0) actually clears those pixels back to
        # transparent (not just "no-op paint") -- since "territory" paints
        # first (see the bottom of this module), a hole here lets whatever
        # real content another group draws for that courtyard (building/
        # road/lawn) show through unobstructed by the blue tint, instead of
        # painting blue first and hoping a later, unrelated group happens
        # to fully cover it.
        for interior in geom.interiors:
            hole_points = [to_px(x, y) for x, y in interior.coords]
            if len(hole_points) >= 3:
                draw.polygon(hole_points, fill=(0, 0, 0, 0))
    elif gtype.startswith("Multi") or gtype == "GeometryCollection":
        for part in geom.geoms:
            _draw_geometry(draw, part, to_px, color, group_key)


def _render_group_worker(
    key: str,
    geoms: list[BaseGeometry],
    px_w: int,
    px_h: int,
    minx: float,
    maxy: float,
    scale: float,
    color: tuple[int, int, int],
) -> tuple[str, bytes]:
    """Draws one legend group's PNG. Plain module-level function taking only
    picklable arguments (no closures, no PIL/shapely objects that don't
    round-trip cleanly) so it can run either inline (small projects) or
    submitted to a worker process (large ones, see render_layer_raster) --
    same code either way, so the two paths can't drift apart."""
    def to_px(x: float, y: float) -> tuple[float, float]:
        return (x - minx) * scale, (maxy - y) * scale

    image = Image.new("RGBA", (px_w, px_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for geom in geoms:
        _draw_geometry(draw, geom, to_px, color, key)
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    return key, buf.getvalue()


def _render_groups_parallel(
    by_group: dict[str, list[BaseGeometry]],
    px_w: int,
    px_h: int,
    minx: float,
    maxy: float,
    scale: float,
    colors: dict[str, tuple[int, int, int]],
) -> dict[str, bytes]:
    """One process per legend group (bounded by _MAX_RENDER_WORKERS/core
    count) instead of one thread doing all of them in sequence. Shapely
    geometries pickle fine on their own (WKB under the hood) -- the actual
    win is that groups have no dependency on each other, so this is
    embarrassingly parallel; it just never used to be split up."""
    worker_count = max(1, min(len(by_group), _MAX_RENDER_WORKERS, os.cpu_count() or 1))
    with ProcessPoolExecutor(max_workers=worker_count) as pool:
        futures = [
            pool.submit(_render_group_worker, key, geoms, px_w, px_h, minx, maxy, scale, colors[key])
            for key, geoms in by_group.items()
        ]
        return dict(future.result() for future in futures)


def render_layer_raster(layers: list[_LayerLike], source_crs: str | None) -> LayerRaster:
    if not layers:
        return LayerRaster(bounds=None, groups=[])

    entries = [(layer_group_key(layer), db_to_shape(layer.geometry)) for layer in layers]

    # The map's "territory" must show the SAME boundary buildable_area()/
    # candidates actually work from, not every raw zone_type="territory"
    # fragment on the layer -- territory_polygon() clusters away pieces with
    # no real infrastructure near them (a stray xref in another CRS, or --
    # live case, 4. Харьковская улица -- an isolated ~54,000 m2 polygon with
    # zero utilities or other zones anywhere near it). Rendering the raw list
    # instead put that discarded piece on the map as a second, unrelated blue
    # blob floating in empty space, which is exactly what confused a live
    # user, and also dragged the canvas bounds out to cover the empty space
    # between the two, making everything else render blurrier than it needs
    # to for no reason (same pixel budget, spread over a much bigger area --
    # see _MAX_CANVAS_PX below).
    entries = [(key, geom) for key, geom in entries if key != "territory"]
    utilities, zones = layers_to_domain(layers)
    territory_geom = None
    try:
        territory_geom = territory_polygon(zones, utilities)
        entries.append(("territory", territory_geom))
    except MissingTerritoryError:
        pass  # nothing to show; other groups still render on their own bounds

    if territory_geom is not None and not territory_geom.is_empty:
        # Anchored on the real work boundary plus a fixed context margin --
        # see _CONTEXT_MARGIN_M for why this replaced "union of every layer".
        tminx, tminy, tmaxx, tmaxy = territory_geom.bounds
        minx, miny = tminx - _CONTEXT_MARGIN_M, tminy - _CONTEXT_MARGIN_M
        maxx, maxy = tmaxx + _CONTEXT_MARGIN_M, tmaxy + _CONTEXT_MARGIN_M
    else:
        # No resolvable territory to anchor on -- fall back to the union of
        # every non-zoning layer (same exclusion as MapView.tsx's old
        # client-side extentBounds: a zoning polygon's real shape can span a
        # whole neighbourhood, so letting it set the canvas would zoom the
        # actual site down to a speck). Zoning is still drawn either way --
        # just clipped to whatever falls inside the canvas everything else
        # defines.
        #
        # A plain union of everything here is itself not safe -- live case,
        # "7. Нижние Поля ул" (this whole branch only runs because there's no
        # territory to anchor on): "unknown" alone carried a genuine second
        # 26 605-object population several km from the real site, plus a
        # handful of far-flung singletons in other categories, and unioning
        # all of it stretched the canvas to a useless ~9x13km span with the
        # real site reduced to a speck. _dominant_cluster_bounds keeps only
        # what's actually near the main mass of the data first, same
        # "biggest coherent cluster wins" principle territory_polygon() uses
        # for its own input -- see its docstring for the calibration.
        bound_geoms = [geom for key, geom in entries if not key.startswith(_ZONING_PREFIX)]
        if not bound_geoms:
            bound_geoms = [geom for _, geom in entries]
        rminx, rminy, rmaxx, rmaxy = _dominant_cluster_bounds(bound_geoms)
        minx, miny = rminx - _CONTEXT_MARGIN_M, rminy - _CONTEXT_MARGIN_M
        maxx, maxy = rmaxx + _CONTEXT_MARGIN_M, rmaxy + _CONTEXT_MARGIN_M

    _, px_w, px_h, scale = _make_pixel_transform(minx, miny, maxx, maxy, _MAX_CANVAS_PX)
    bounds = _wgs84_bounds(minx, miny, maxx, maxy, source_crs)

    by_group: dict[str, list[BaseGeometry]] = {}
    for key, geom in entries:
        by_group.setdefault(key, []).append(geom)
    # existing_greenery/existing_lawn always get a legend row, even at 0
    # objects -- every other category here stays "only what's actually
    # present" (see this function's own comment on _CONTEXT_MARGIN_M/
    # extentBounds for that general philosophy), but these two specifically
    # were the subject of a live, repeated complaint ("не рисуется газон
    # который уже есть") that turned out to mean two different things on two
    # different streets: on one, existing_lawn genuinely has zero objects
    # (see layer_rules.py's docstring on why a project's own proposed "Газон"
    # layers are correctly left unclassified rather than guessed as real
    # existing turf); on another, existing_greenery has thousands of real
    # objects that were simply camouflaged by color (see _ZONE_COLORS' own
    # comment above). A silently-absent toggle reads as "the feature is
    # broken", not "this street's data genuinely has none" -- showing "0"
    # here makes that a visible fact instead of an ambiguous gap.
    by_group.setdefault("existing_greenery", [])
    by_group.setdefault("existing_lawn", [])
    colors = {key: _hex_to_rgb(_group_color(key)) for key in by_group}

    if len(entries) >= _PARALLEL_RENDER_THRESHOLD and len(by_group) > 1:
        pngs = _render_groups_parallel(by_group, px_w, px_h, minx, maxy, scale, colors)
    else:
        pngs = dict(
            _render_group_worker(key, geoms, px_w, px_h, minx, maxy, scale, colors[key])
            for key, geoms in by_group.items()
        )

    groups = [
        LayerRasterGroup(key=key, label=_group_label(key), color=_group_color(key), count=len(geoms), png=pngs[key])
        for key, geoms in by_group.items()
    ]
    # Paint order, not just list order: MapView.tsx renders one <ImageOverlay>
    # per group in this exact array order, and later ImageOverlays paint over
    # earlier ones. Plain alphabetical-by-label sorting put "Территория"
    # (blue solid fill over the whole work boundary) after "Здания" on real
    # data -- the territory wash then painted OVER the building outlines,
    # visually burying them under a uniform blue even though buildable_area()
    # correctly excludes them from the actual generated plan underneath.
    # Found live: a user screenshot showing a big blue rectangle with no
    # visible buildings, right where the real "Проектное решение" reference
    # plan shows two building footprints. Territory is a backdrop -- it must
    # paint first (bottom), same idea as a basemap sitting under everything
    # else -- everything else keeps its existing alphabetical order among
    # itself, only territory's position is pinned.
    groups.sort(key=lambda g: (g.key != "territory", g.label))
    return LayerRaster(bounds=bounds, groups=groups)


_MAX_CACHE_ENTRIES = 16
_lock = threading.Lock()
_cache: OrderedDict[str, LayerRaster] = OrderedDict()


async def get_layer_raster(project) -> LayerRaster:
    """Cached wrapper -- see the module docstring for why no invalidation key
    is needed. The lock only ever guards the plain dict read/write, never the
    render itself (a real wait -- CPU-bound PIL work, run off the event loop)
    -- holding a threading.Lock across an `await` would stall every other
    request while this one's cache is still filling."""
    with _lock:
        if project.id in _cache:
            _cache.move_to_end(project.id)
            return _cache[project.id]

    result = await run_in_threadpool(render_layer_raster, project.layers, display_crs(project))

    with _lock:
        _cache[project.id] = result
        while len(_cache) > _MAX_CACHE_ENTRIES:
            _cache.popitem(last=False)
        return result
