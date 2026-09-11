"""In-process LRU caches of two things derived from a project's layers that
every manual edit needs: the setback exclusion zone per planting type, and
the territory boundary.

/validate now runs after every manual edit (the map paints violating items
red), and building a zone -- geo_engine.buffers.build_exclusion_zone, a
buffer-and-union over every utility and zone -- is the part that grows with
a real utility network. It doesn't need rebuilding per call: a project's
layers are immutable after upload, and the rulebook only changes when
planting_norms.yaml is edited, which the key tracks via the file's mtime
(so editing the YAML invalidates without a restart). Bounded by
_MAX_ENTRIES; a zone for a real territory is on the order of a few MB.

The territory boundary needs no such invalidation key -- it depends only on
layers, not on planting_norms.yaml -- but every single move/restore/patch
edit looks it up (to reject placing something outside the plot), so without
caching it'd re-run layers_to_domain over the whole layer set on every edit.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import TypeVar

import shapely
from shapely.geometry.base import BaseGeometry

from backend.app.core.config import settings
from backend.app.db.models import Project
from backend.app.services.geo_io import layers_to_domain
from geo_engine.buffers import build_exclusion_zone
from geo_engine.norms import load_norms

T = TypeVar("T")

_MAX_ENTRIES = 24  # ~8 recently validated projects x 3 planting types
_MAX_TERRITORY_ENTRIES = 64  # one entry per project, not per planting type -- cheaper to keep more around

_lock = threading.Lock()
_cache: OrderedDict[tuple[str, str, int], BaseGeometry | None] = OrderedDict()
_territory_cache: OrderedDict[str, BaseGeometry] = OrderedDict()


def _get_or_build_locked(project: Project, planting_type: str) -> BaseGeometry | None:
    key = (str(project.id), planting_type, settings.planting_norms_path.stat().st_mtime_ns)
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]

    utilities, zones = layers_to_domain(project.layers)
    zone = build_exclusion_zone(utilities, zones, planting_type, load_norms(settings.planting_norms_path))
    if zone is not None and not zone.is_empty:
        shapely.prepare(zone)
    _cache[key] = zone
    while len(_cache) > _MAX_ENTRIES:
        _cache.popitem(last=False)
    return zone


def with_exclusion_zone(project: Project, planting_type: str, fn: Callable[[BaseGeometry | None], T]) -> T:
    """Runs `fn(zone)` while holding the cache lock. The cached zone is
    GEOS-prepared (fast point-in-zone checks), and a prepared geometry's
    lazily-built internal index isn't guaranteed safe to query from several
    run_in_threadpool workers at once -- so uses are serialized. Cheap in
    practice: the expensive part, building the zone, happens once per key.
    """
    with _lock:
        return fn(_get_or_build_locked(project, planting_type))


def _get_or_build_territory_locked(project: Project) -> BaseGeometry:
    # Imported here, not at module level: pipeline_service doesn't import
    # this module, so there's no real cycle, but keeping it local documents
    # that the dependency only goes one way.
    from backend.app.services.pipeline_service import territory_polygon

    key = str(project.id)
    if key in _territory_cache:
        _territory_cache.move_to_end(key)
        return _territory_cache[key]

    _, zones = layers_to_domain(project.layers)
    territory = territory_polygon(zones)
    shapely.prepare(territory)
    _territory_cache[key] = territory
    while len(_territory_cache) > _MAX_TERRITORY_ENTRIES:
        _territory_cache.popitem(last=False)
    return territory


def with_territory(project: Project, fn: Callable[[BaseGeometry], T]) -> T:
    """Runs `fn(territory)` while holding the cache lock -- same shape and
    same reason as `with_exclusion_zone` (a prepared geometry's lazily-built
    index isn't guaranteed safe to query concurrently from several
    run_in_threadpool workers)."""
    with _lock:
        return fn(_get_or_build_territory_locked(project))
