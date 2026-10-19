"""In-process store of background "generate plan" jobs.

POST .../generate used to block the whole request for however long
candidate generation + greedy placement takes -- fine on the 100x80m demo
synthetic territory, but measurements on a real ~1.5x1.5km
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

Остановка и частичные результаты (п. 8 редизайна): «Остановить» сразу переводит
задачу в `cancelled` (интерфейс освобождается мгновенно), а сам расчёт доходит
до ближайшей точки опроса `should_stop` и бросает `GenerationCancelled`;
результат не сохраняется. Посадки каждого типа складываются в `partial` по мере
готовности — фронт подмешивает их в карту, пока план ещё считается.
"""

from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Literal

JobStatus = Literal["pending", "done", "error", "cancelled"]

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
    # «Остановить» нажато: расчёт (в потоке или в чужих процессах) смотрит на это.
    cancel_requested: bool = False
    # Посадки уже посчитанных типов (доменные PlantingItem, до пост-обработки):
    # тип -> список. Версия растёт с каждым новым типом — фронт перечитывает
    # частичный результат только когда она сменилась.
    partial: dict[str, list] = field(default_factory=dict)
    partial_version: int = 0


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


def add_partial(job_id: str, planting_type: str, items: list) -> None:
    """Вызывается из потока расчёта; присваивание в dict и рост версии — под GIL."""
    job = _jobs.get(job_id)
    if job is None or job.cancel_requested:
        return
    job.partial[planting_type] = items
    job.partial_version += 1


def partial_counts(job: GenerationJob) -> dict[str, int]:
    return {planting_type: len(items) for planting_type, items in dict(job.partial).items()}


def _release_partial(job: GenerationJob) -> None:
    """Частичный результат больше не нужен (план готов, остановлен, упал):
    это десятки тысяч доменных объектов, держать их до вытеснения задачи — зря."""
    job.partial = {}


def mark_done(job_id: str, plan_id: str) -> None:
    job = _jobs.get(job_id)
    if job is not None and job.status == "pending":
        job.status = "done"
        job.plan_id = plan_id
        _release_partial(job)


def mark_error(job_id: str, error: str) -> None:
    job = _jobs.get(job_id)
    if job is not None and job.status == "pending":
        job.status = "error"
        job.error = error
        _release_partial(job)


def request_cancel(job_id: str) -> str | None:
    """«Остановить». Возвращает статус задачи после вызова (None — задачи нет).
    Остановить можно только идущую: у готовой план уже в проекте. Вызывается на
    цикле событий, так же как и фиксация плана в `generate_plan` (от проверки
    `should_stop` до добавления в проект там нет ни одного await), поэтому
    «остановлено, но план всё же появился» невозможно."""
    job = _jobs.get(job_id)
    if job is None:
        return None
    if job.status == "pending":
        job.cancel_requested = True
        job.status = "cancelled"
        _release_partial(job)
    return job.status


def should_stop(job_id: str) -> bool:
    job = _jobs.get(job_id)
    return job is None or job.cancel_requested


def get_job(job_id: str) -> GenerationJob | None:
    return _jobs.get(job_id)
