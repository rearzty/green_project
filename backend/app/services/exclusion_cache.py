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
from geo_engine.planner import CROWN_SPACING_TYPES
from geo_engine.species import load_catalogue
from geo_engine.territory import MissingTerritoryError

T = TypeVar("T")

_MAX_ENTRIES = 24  # ~8 recently validated projects x 3 planting types
_MAX_TERRITORY_ENTRIES = 64  # one entry per project, not per planting type -- cheaper to keep more around

# (project_id, planting_type, species, mtime planting_norms.yaml). Порода в
# ключе только у деревьев — у остальных типов отступ от неё не зависит (см.
# _species_key), иначе кэш дробился бы на копии одной и той же зоны.
ZoneKey = tuple[str, str, str | None, bool, int]

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


def _species_key(planting_type: str, species: str | None) -> str | None:
    """Порода влияет на отступ только у деревьев: МГСН 1.02-02 п. 4.2.8 и
    743-ПП прим. 1/3 — про деревья, и `norms.resolve_setback` для остальных
    типов породу не смотрит (та же граница, что `planner.CROWN_SPACING_TYPES`)."""
    return species if planting_type in CROWN_SPACING_TYPES else None


def _zone_key(project: Project, planting_type: str, species: str | None, trees_in_paving: bool = False) -> ZoneKey:
    return (
        str(project.id),
        planting_type,
        _species_key(planting_type, species),
        trees_in_paving,
        settings.planting_norms_path.stat().st_mtime_ns,
    )


def _get_or_build_locked(key: ZoneKey, project: Project, planting_type: str) -> BaseGeometry | None:
    with _struct_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]

    utilities, zones = layers_to_domain(project.layers)
    # Порода — та же, что ставил генератор, иначе проверка мерила бы по
    # табличному отступу, а генератор — по увеличенному для широкой кроны, и
    # ручная правка могла бы подвинуть дерево ближе нормы без подсветки
    # нарушения. CLI-проверка (geo_engine.compliance) породу учитывает всегда.
    catalogue = load_catalogue()
    species = catalogue.get(key[2])
    norms = load_norms(settings.planting_norms_path)
    if key[3]:
        norms = norms.with_trees_in_paving()

    def build(territory: BaseGeometry | None) -> BaseGeometry:
        return build_exclusion_zone(
            utilities, zones, planting_type, norms, species, catalogue.crown_reference_diameter_m, clip_to=territory
        )

    # Обрезка по участку — внутри блокировки участка: его геометрия общая и
    # подготовленная, а GEOS не обещает безопасного параллельного чтения
    # (см. докстринг модуля). Без участка — полная зона, как раньше.
    try:
        zone = with_territory(project, build)
    except MissingTerritoryError:
        zone = build(None)
    if zone is not None and not zone.is_empty:
        shapely.prepare(zone)

    with _struct_lock:
        _cache[key] = zone
        while len(_cache) > _MAX_ENTRIES:
            evicted_key, _ = _cache.popitem(last=False)
            _zone_locks.pop(evicted_key, None)
    return zone


def with_exclusion_zone(
    project: Project,
    planting_type: str,
    fn: Callable[[BaseGeometry | None], T],
    species: str | None = None,
    trees_in_paving: bool = False,
) -> T:
    """Runs `fn(zone)` while holding a lock scoped to this (project,
    planting_type, norms version) key only -- see the module docstring for
    why that's safe and why it used to be one lock for every key.
    """
    key = _zone_key(project, planting_type, species, trees_in_paving)
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
    territory = territory_polygon(zones, utilities, allow_order_boundary=project.use_order_boundary)
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
