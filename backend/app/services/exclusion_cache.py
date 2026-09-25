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

Locking is two-tiered. `_struct_lock` is held only for the dict bookkeeping
(read/insert/evict on the OrderedDicts and the per-key lock registries) --
always a handful of Python operations, never held across a GEOS call.
`with_exclusion_zone`/`with_territory` additionally hold a lock scoped to
their own (project, planting_type[, norms version]) key while a caller uses
the geometry, because a GEOS-prepared geometry's lazily-built internal index
isn't guaranteed safe to query concurrently from several run_in_threadpool
workers at once. That used to be a *single* lock shared across every key --
correct, but it meant a validate on one project waited behind a compliance
report being computed on a completely unrelated project, or even behind the
same project's *other* planting type's zone. Per-key locking keeps the same
safety guarantee (queries against one specific prepared geometry are still
serialized) without serializing work that never touched each other.
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

ZoneKey = tuple[str, str, int]

_struct_lock = threading.Lock()
_cache: OrderedDict[ZoneKey, BaseGeometry | None] = OrderedDict()
_territory_cache: OrderedDict[str, BaseGeometry] = OrderedDict()
# One threading.Lock per cache key, created lazily. Evicted alongside its
# cache entry (see the two _evict_* helpers) so this doesn't grow without
# bound across a long-running process the way the cache itself wouldn't --
# a stray lock left behind after eviction is harmless (whoever still holds a
# reference keeps using it fine), just untidy.
_zone_locks: dict[ZoneKey, threading.Lock] = {}
_territory_locks: dict[str, threading.Lock] = {}


def _lock_for(registry: dict, key) -> threading.Lock:
    with _struct_lock:
        lock = registry.get(key)
        if lock is None:
            lock = threading.Lock()
            registry[key] = lock
        return lock


def _zone_key(project: Project, planting_type: str) -> ZoneKey:
    return (str(project.id), planting_type, settings.planting_norms_path.stat().st_mtime_ns)


def _get_or_build_locked(key: ZoneKey, project: Project, planting_type: str) -> BaseGeometry | None:
    with _struct_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]

    utilities, zones = layers_to_domain(project.layers)
    zone = build_exclusion_zone(utilities, zones, planting_type, load_norms(settings.planting_norms_path))
    if zone is not None and not zone.is_empty:
        shapely.prepare(zone)

    with _struct_lock:
        _cache[key] = zone
        while len(_cache) > _MAX_ENTRIES:
            evicted_key, _ = _cache.popitem(last=False)
            _zone_locks.pop(evicted_key, None)
    return zone


def with_exclusion_zone(project: Project, planting_type: str, fn: Callable[[BaseGeometry | None], T]) -> T:
    """Runs `fn(zone)` while holding a lock scoped to this (project,
    planting_type, norms version) key only -- see the module docstring for
    why that's safe and why it used to be one lock for every key.
    """
    key = _zone_key(project, planting_type)
    with _lock_for(_zone_locks, key):
        zone = _get_or_build_locked(key, project, planting_type)
        return fn(zone)


def _get_or_build_territory_locked(project: Project) -> BaseGeometry:
    # Imported here, not at module level: pipeline_service doesn't import
    # this module, so there's no real cycle, but keeping it local documents
    # that the dependency only goes one way.
    from backend.app.services.pipeline_service import territory_polygon

    key = str(project.id)
    with _struct_lock:
        if key in _territory_cache:
            _territory_cache.move_to_end(key)
            return _territory_cache[key]

    utilities, zones = layers_to_domain(project.layers)
    territory = territory_polygon(zones, utilities)
    shapely.prepare(territory)

    with _struct_lock:
        _territory_cache[key] = territory
        while len(_territory_cache) > _MAX_TERRITORY_ENTRIES:
            evicted_key, _ = _territory_cache.popitem(last=False)
            _territory_locks.pop(evicted_key, None)
    return territory


def with_territory(project: Project, fn: Callable[[BaseGeometry], T]) -> T:
    """Runs `fn(territory)` while holding a lock scoped to this project only
    -- same reasoning as `with_exclusion_zone`."""
    key = str(project.id)
    with _lock_for(_territory_locks, key):
        territory = _get_or_build_territory_locked(project)
        return fn(territory)
