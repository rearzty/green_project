"""In-process store of background "upload project" jobs.

POST /api/projects used to block the whole request for however long DWG
conversion + DXF/bundle parsing takes -- on a real multi-file ZIP bundle this
was measured (see CLAUDE.md's ACIS-lookup/redundant-read profiling on
"4. Харьковская улица") at over a hundred seconds even after those fixes,
well past what a browser tab or a proxy will hold a connection open for. Now
the route saves the uploaded bytes (unavoidably tied to the request's own
body) and hands back a job id immediately; the frontend polls
GET /api/projects/upload/{job_id} for completion, then fetches the finished
project through the existing GET /api/projects/{project_id}. Same shape as
generation_jobs.py/export_jobs.py -- see generation_jobs.py's own docstring
for why in-memory/single-process/bounded is enough here too.
"""

from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Literal

JobStatus = Literal["pending", "done", "error"]

_MAX_JOBS = 200

_jobs: OrderedDict[str, "ProjectUploadJob"] = OrderedDict()


@dataclass
class ProjectUploadJob:
    status: JobStatus = "pending"
    project_id: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.monotonic)


def create_job() -> str:
    job_id = str(uuid.uuid4())
    _jobs[job_id] = ProjectUploadJob()
    while len(_jobs) > _MAX_JOBS:
        _jobs.popitem(last=False)
    return job_id


def mark_done(job_id: str, project_id: str) -> None:
    job = _jobs.get(job_id)
    if job is not None:
        job.status = "done"
        job.project_id = project_id


def mark_error(job_id: str, error: str) -> None:
    job = _jobs.get(job_id)
    if job is not None:
        job.status = "error"
        job.error = error


def get_job(job_id: str) -> ProjectUploadJob | None:
    return _jobs.get(job_id)
