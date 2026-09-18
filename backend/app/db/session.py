"""In-process, in-memory store of every Project this backend currently holds.

No database at all -- see docs/decision_log.md for why. Everything lives only
as long as this process does: restart the backend and it's gone, by design
("загрузили - поработали в одной сессии - забыли"). A single lock guards all
access -- every operation here is plain dict/list manipulation with no I/O,
so a single global lock adds no meaningful contention, and avoids reasoning
about per-project locking for what is, in practice, a handful of
concurrently-open projects during a hackathon demo.

That said, "a handful" was never enforced -- this used to be a plain dict
with no cap at all, unlike every derived cache in the codebase
(layer_raster.py's PNG cache, exclusion_cache.py's zone/territory caches),
which all bound themselves by entry count. Unlike those, evicting here loses
real data irrecoverably (there's nothing to recompute it from), so eviction
is deliberately conservative: it only runs when a new project is saved (the
one place unbounded growth actually originates -- generation/editing on an
already-held project is already self-bounded per-project by
pipeline_service._prune_stale_plans), it always keeps the most-recently-used
project no matter how large, and it evicts oldest-touched first so a project
someone is still actively working with survives.
"""

from __future__ import annotations

from collections import OrderedDict
import threading

from backend.app.db.models import Project

_lock = threading.Lock()
_projects: OrderedDict[str, Project] = OrderedDict()

# Object count across every held project's layers + materialized plan items
# -- not bytes. Profiling actual Python object sizes recursively would cost
# more than this check saves, and object count is the metric the rest of
# this codebase already uses to talk about scale (worklog.md/CLAUDE.md:
# "1. Олимпийская деревня" is described as "371,685 объектов", never as a
# byte figure). 2,000,000 comfortably fits several real pilot streets (the
# largest measured so far, ~372K layers) or many small synthetic projects at
# once, while still bounding a long demo day of repeated real uploads to a
# few hundred MB of process memory instead of letting it grow forever.
_MAX_TOTAL_WEIGHT = 2_000_000


def _project_weight(project: Project) -> int:
    return len(project.layers) + sum(len(plan.items) for plan in project.plans)


def _evict_locked() -> None:
    total = sum(_project_weight(p) for p in _projects.values())
    while total > _MAX_TOTAL_WEIGHT and len(_projects) > 1:
        evicted_id, evicted = _projects.popitem(last=False)
        total -= _project_weight(evicted)
        # Not silent -- losing a project a user might come back to (the
        # frontend's localStorage session restore assumes it's still there)
        # is a real behavioural change from "lives until restart", worth a
        # line even without a dedicated logger elsewhere in this codebase
        # (see project_service.py's own `print(f"  ! ...")` for unreadable
        # bundle files -- same "lose it, but say so" convention).
        print(f"  ! эвикшн проекта {evicted_id} ({evicted.name!r}) из памяти — общий объём хранимых проектов превысил лимит")


def save_project(project: Project) -> None:
    with _lock:
        _projects[project.id] = project
        _projects.move_to_end(project.id)
        _evict_locked()


def get_project(project_id: str) -> Project | None:
    with _lock:
        project = _projects.get(project_id)
        if project is not None:
            _projects.move_to_end(project_id)
        return project
