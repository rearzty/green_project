from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from backend.app.api.deps import PlanDep, get_plan_or_404
from backend.app.schemas.plan import ExportJobOut, ExportJobStatus, ExportOptionsIn, ExportSummaryOut
from backend.app.services import export_jobs
from backend.app.services.export_service import ALL_LAYER_KEYS, EmptyExportError, ExportOptions, export_summary, plan_to_package

router = APIRouter(prefix="/api/projects/{project_id}/plans/{plan_id}", tags=["export"])
logger = logging.getLogger(__name__)

# Same rationale as routes_generate.py's _background_tasks: without a live
# reference somewhere, asyncio is free to garbage-collect an in-flight task.
_background_tasks: set[asyncio.Task] = set()


def _options(body: ExportOptionsIn | None) -> ExportOptions:
    if body is None:
        return ExportOptions()
    return ExportOptions(
        layers=frozenset(body.layers if body.layers is not None else ALL_LAYER_KEYS),
        include_base=body.include_base,
        include_report=body.include_report,
    )


async def _run_export_job(job_id: str, project_id: str, plan_id: str, options: ExportOptions) -> None:
    """Runs on the event loop after POST /export-dxf has already returned."""
    try:
        plan = await get_plan_or_404(project_id, plan_id)
        # Архив, а не голый .dxf: план поверх исходного чертежа плюс файлы его
        # внешних ссылок и отчёт обоснований — см. export_service.py.
        output_path = Path(tempfile.gettempdir()) / f"export_{job_id}.zip"
        await run_in_threadpool(plan_to_package, plan, output_path, options)
        export_jobs.mark_done(job_id, output_path, f"planting_plan_{plan_id}.zip")
    except HTTPException as exc:
        export_jobs.mark_error(job_id, str(exc.detail))
    except EmptyExportError as exc:
        export_jobs.mark_error(job_id, str(exc))
    except Exception:
        logger.exception("Background DXF export failed (job_id=%s, plan_id=%s)", job_id, plan_id)
        export_jobs.mark_error(job_id, "Не удалось экспортировать DXF — внутренняя ошибка сервера.")


@router.get("/export-summary", response_model=ExportSummaryOut)
async def export_summary_route(plan: PlanDep) -> ExportSummaryOut:
    """Диалог «Перед экспортом»: нарушения, несверенные ссылки на нормы, слои
    файла с числом объектов и признак «исходный чертёж доступен». Один проход
    `explain_items` — синхронно, как отчёт обоснований."""
    return ExportSummaryOut(**await run_in_threadpool(export_summary, plan))


@router.post("/export-dxf", response_model=ExportJobOut, status_code=202)
async def export_dxf(project_id: str, plan_id: str, body: ExportOptionsIn | None = None) -> ExportJobOut:
    """Starts DXF export as a background job instead of blocking this
    request -- writing a real-scale plan can take minutes (see CLAUDE.md).
    The frontend polls GET .../export-dxf/{job_id}, then downloads through
    GET .../export-dxf/{job_id}/download once done."""
    options = _options(body)
    if not options.layers:
        raise HTTPException(status_code=400, detail="Не выбран ни один слой — в файле нечего будет показать.")
    job_id = export_jobs.create_job()
    task = asyncio.create_task(_run_export_job(job_id, project_id, plan_id, options))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return ExportJobOut(job_id=job_id)


@router.get("/export-dxf/{job_id}", response_model=ExportJobStatus)
async def export_dxf_status(project_id: str, plan_id: str, job_id: str) -> ExportJobStatus:
    # No plan lookup here on purpose -- same reasoning as
    # routes_generate.py's generate_status: polled every ~second, and job_id
    # already fully identifies the job.
    job = export_jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задача экспорта не найдена — возможно, сервер перезапускался.")
    return ExportJobStatus(status=job.status, error=job.error)


@router.get("/export-dxf/{job_id}/download")
async def export_dxf_download(project_id: str, plan_id: str, job_id: str, background_tasks: BackgroundTasks) -> FileResponse:
    job = export_jobs.get_job(job_id)
    if job is None or job.status != "done" or job.output_path is None:
        raise HTTPException(status_code=404, detail="Файл экспорта не найден — возможно, задача ещё не завершена или сервер перезапускался.")
    # discard() unlinks the file -- must run only *after* FileResponse has
    # finished streaming it (BackgroundTasks guarantees that), not inline
    # here, which would race the response and could delete the file out
    # from under an in-flight read.
    background_tasks.add_task(export_jobs.discard, job_id)
    return FileResponse(path=job.output_path, media_type="application/zip", filename=job.download_filename)
