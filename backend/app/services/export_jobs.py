"""In-process store of background "export DXF" jobs.

GET .../export.dxf used to write the whole DXF file inline in the request
(via run_in_threadpool, so at least the event loop wasn't blocked) -- fine
on synthetic-scale plans, but CLAUDE.md's own benchmark on a real-scale plan
(hundreds of thousands of items) put this at up to ~3 minutes, which risks
tripping a browser/proxy timeout on a single held-open request the same way
plan generation did (see generation_jobs.py). Now POST .../export-dxf hands
back a job id immediately, the frontend polls GET .../export-dxf/{job_id}
for completion, then downloads the finished file through
GET .../export-dxf/{job_id}/download.

In-memory only, not persisted, not shared across processes -- same
single-uvicorn-process assumption as generation_jobs.py. Unlike a
generation job, a finished export job owns a real temp file on disk, so
eviction (by _MAX_JOBS or explicit cleanup after download) also deletes it
-- otherwise an abandoned job (tab closed before downloading) would leak a
multi-hundred-MB file forever.
"""

from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

JobStatus = Literal["pending", "done", "error"]

_MAX_JOBS = 50  # lower than generation_jobs' cap -- each entry can own a real file on disk

_jobs: OrderedDict[str, "ExportJob"] = OrderedDict()


@dataclass
class ExportJob:
    status: JobStatus = "pending"
    output_path: Path | None = None
    download_filename: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.monotonic)


def _delete_file(job: "ExportJob") -> None:
    if job.output_path is not None:
        job.output_path.unlink(missing_ok=True)


def create_job() -> str:
    job_id = str(uuid.uuid4())
    _jobs[job_id] = ExportJob()
    while len(_jobs) > _MAX_JOBS:
        _, evicted = _jobs.popitem(last=False)
        _delete_file(evicted)
    return job_id


def mark_done(job_id: str, output_path: Path, download_filename: str) -> None:
    job = _jobs.get(job_id)
    if job is not None:
        job.status = "done"
        job.output_path = output_path
        job.download_filename = download_filename


def mark_error(job_id: str, error: str) -> None:
    job = _jobs.get(job_id)
    if job is not None:
        job.status = "error"
        job.error = error


def get_job(job_id: str) -> ExportJob | None:
    return _jobs.get(job_id)


def discard(job_id: str) -> None:
    """Called after the finished file has been sent to the client -- frees
    the temp file immediately instead of waiting for _MAX_JOBS eviction."""
    job = _jobs.pop(job_id, None)
    if job is not None:
        _delete_file(job)
