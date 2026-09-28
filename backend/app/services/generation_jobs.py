"""In-process store of background "generate plan" jobs.

POST .../generate used to block the whole request for however long
candidate generation + greedy placement takes -- fine on the 100x80m demo
synthetic territory, but CLAUDE.md's own measurements on a real ~1.5x1.5km
territory put this at tens of seconds even after the O(n log n) rewrite of
placement.py/candidates.py, which risks tripping a browser/proxy timeout on
real data. Now the route hands back a job id immediately and the frontend
polls GET .../generate/{job_id} for completion, then fetches the finished
plan through the existing GET .../plans/{plan_id}.

In-memory only, not persisted, not shared across processes -- fine for a
single uvicorn process (no --workers>1); a multi-process/multi-replica
deployment would need a real queue (e.g. Celery/RQ + Redis) instead. Bounded
by _MAX_JOBS so a long session of repeated generate clicks can't grow this
forever.
"""

from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Literal

JobStatus = Literal["pending", "done", "error"]

_MAX_JOBS = 200

_jobs: OrderedDict[str, "GenerationJob"] = OrderedDict()


@dataclass
class GenerationJob:
    status: JobStatus = "pending"
    plan_id: str | None = None
    error: str | None = None
    # Same idea and shape as project_jobs.ProjectUploadJob's stage/progress --
    # see that module's docstring. Here `stage` is which planting_type just
    # finished (geo_engine.planner.plan_items reports whole types, not
    # candidate-level counts -- see its own on_progress docstring for why).
    stage: str | None = None
    progress: float | None = None
    created_at: float = field(default_factory=time.monotonic)


def create_job() -> str:
    job_id = str(uuid.uuid4())
    _jobs[job_id] = GenerationJob()
    while len(_jobs) > _MAX_JOBS:
        _jobs.popitem(last=False)
    return job_id


_TYPE_LABELS = {"tree": "Деревья", "shrub": "Кустарники", "lawn": "Газон"}


def update_progress(job_id: str, planting_type: str, done: int, total: int) -> None:
    job = _jobs.get(job_id)
    if job is None:
        return
    label = _TYPE_LABELS.get(planting_type, planting_type)
    job.stage = f"Готово: {label} ({done} из {total})" if total > 1 else f"Готово: {label}"
    job.progress = (done / total) if total > 0 else None


def mark_done(job_id: str, plan_id: str) -> None:
    job = _jobs.get(job_id)
    if job is not None:
        job.status = "done"
        job.plan_id = plan_id


def mark_error(job_id: str, error: str) -> None:
    job = _jobs.get(job_id)
    if job is not None:
        job.status = "error"
        job.error = error


def get_job(job_id: str) -> GenerationJob | None:
    return _jobs.get(job_id)
