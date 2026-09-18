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

from backend.app.services.geo_io import _transformer_to_wgs84, db_to_shape


class _LayerLike(Protocol):
    """What render_layer_raster actually needs."""

    kind: str
    object_type: str
    attrs: dict | None
    geometry: object

# Mirrors frontend/lib/mapStyle.ts's UTILITY_COLOR/ZONE_COLORS/
# ZONING_CATEGORY_COLORS/LAYER_TYPE_LABELS/ZONING_CATEGORY_LABELS exactly —
# two copies (Python here, TS there) because the raster is drawn server-side
# and the legend swatches are drawn client-side, and there's no shared config
# file either language reads today. Keep the two in sync by hand if either
# changes.
_UTILITY_COLOR = "#b91c1c"
_ZONE_COLORS = {
    "building": "#78716c",
    "road": "#57534e",
    "territory": "#0ea5e9",
    "existing_greenery": "#16a34a",
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
_LINE_WIDTH_PX = {"utility": 3}
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
    # ((south, west), (north, east)) in WGS84, or None for an empty project —
    # same shape react-leaflet's <ImageOverlay bounds=.../> expects directly.
    bounds: tuple[tuple[float, float], tuple[float, float]] | None
    groups: list[LayerRasterGroup]


def _bounds_of(geoms: list[BaseGeometry]) -> tuple[float, float, float, float]:
    arr = shapely.bounds(np.array(geoms, dtype=object))
    return float(arr[:, 0].min()), float(arr[:, 1].min()), float(arr[:, 2].max()), float(arr[:, 3].max())


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
        # Interior rings (courtyards/holes) aren't punched out -- this is a
        # backdrop, not a precision fill, and real holes are rare in this
        # dataset's buildings/zones; not worth the extra compositing pass.
        points = [to_px(x, y) for x, y in geom.exterior.coords]
        if len(points) >= 3:
            draw.polygon(points, fill=(*color, _FILL_ALPHA), outline=color)
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

    # Same exclusion as MapView.tsx's extentBounds: a zoning polygon's real
    # shape can span a whole neighbourhood, so letting it set the canvas
    # would zoom the actual site down to a speck. Zoning is still drawn --
    # just clipped to whatever falls inside the canvas everything else defines.
    bound_geoms = [geom for key, geom in entries if not key.startswith(_ZONING_PREFIX)]
    if not bound_geoms:
        bound_geoms = [geom for _, geom in entries]
    minx, miny, maxx, maxy = _bounds_of(bound_geoms)

    _, px_w, px_h, scale = _make_pixel_transform(minx, miny, maxx, maxy, _MAX_CANVAS_PX)
    bounds = _wgs84_bounds(minx, miny, maxx, maxy, source_crs)

    by_group: dict[str, list[BaseGeometry]] = {}
    for key, geom in entries:
        by_group.setdefault(key, []).append(geom)
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
    groups.sort(key=lambda g: g.label)
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

    result = await run_in_threadpool(render_layer_raster, project.layers, project.source_crs)

    with _lock:
        _cache[project.id] = result
        while len(_cache) > _MAX_CACHE_ENTRIES:
            _cache.popitem(last=False)
        return result
