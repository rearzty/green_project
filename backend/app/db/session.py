"""In-process, in-memory store of every Project this backend currently holds.

No database at all -- see docs/decision_log.md for why. Everything lives only
as long as this process does: restart the backend and it's gone, by design
("загрузили - поработали в одной сессии - забыли"). A single lock guards all
access -- every operation here is plain dict/list manipulation with no I/O,
so a single global lock adds no meaningful contention, and avoids reasoning
about per-project locking for what is, in practice, a handful of
concurrently-open projects during a hackathon demo.
"""

from __future__ import annotations

import threading

from backend.app.db.models import Project

_lock = threading.Lock()
_projects: dict[str, Project] = {}


def save_project(project: Project) -> None:
    with _lock:
        _projects[project.id] = project


def get_project(project_id: str) -> Project | None:
    with _lock:
        return _projects.get(project_id)
