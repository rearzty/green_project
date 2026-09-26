from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile

from backend.app.api.deps import ProjectDep, ProjectMetaDep
from backend.app.schemas.geo import GeoJSONFeatureCollection
from backend.app.schemas.project import (
    LayersRasterGroupOut,
    LayersRasterOut,
    ProjectOut,
    ProjectUploadJobOut,
    ProjectUploadJobStatus,
)
from backend.app.services import project_jobs
from backend.app.services.geo_io import display_crs, layers_to_feature_collection
from backend.app.services.layer_raster import get_layer_raster
from backend.app.services.project_service import UnsupportedFileTypeError, create_project_from_file

router = APIRouter(prefix="/api/projects", tags=["projects"])
logger = logging.getLogger(__name__)

# Same reason as routes_generate.py's own set: without a strong reference
# somewhere, asyncio only guarantees a task survives while something holds
# one -- an upload job with no live reference could be garbage-collected
# mid-parse, silently abandoning it.
_background_tasks: set[asyncio.Task] = set()


async def _run_upload_job(job_id: str, name: str, tmp_path: Path, source_crs: str | None) -> None:
    """Runs on the event loop after POST /api/projects has already returned.
    Owns tmp_path's lifetime -- the route only creates it, this cleans it up
    on every exit path."""
    try:
        project = await create_project_from_file(name=name, upload_path=tmp_path, source_crs=source_crs)
        project_jobs.mark_done(job_id, project.id)
    except UnsupportedFileTypeError as exc:
        project_jobs.mark_error(job_id, str(exc))
    except Exception:
        logger.exception("Background project upload failed (job_id=%s)", job_id)
        project_jobs.mark_error(job_id, "Не удалось загрузить проект — внутренняя ошибка сервера.")
    finally:
        tmp_path.unlink(missing_ok=True)


@router.post("", response_model=ProjectUploadJobOut, status_code=202)
async def upload_project(
    name: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
    source_crs: Annotated[str | None, Form()] = None,
) -> ProjectUploadJobOut:
    """Starts DWG conversion + bundle parsing as a background job instead of
    blocking this request -- a real multi-file ZIP bundle can take well over
    a hundred seconds (see CLAUDE.md), long past a browser/proxy timeout. The
    frontend polls GET .../upload/{job_id} for completion, then fetches the
    finished project through the existing GET .../{project_id}.

    Reading the upload body into a temp file stays inline: it's the one part
    genuinely tied to this request's own lifetime (the file bytes only exist
    on the wire while it's open), and it's ordinary I/O, not the CPU-bound
    parsing this background job exists to get off the browser's back.
    """
    suffix = Path(file.filename or "").suffix.lower()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)

    job_id = project_jobs.create_job()
    task = asyncio.create_task(_run_upload_job(job_id, name, tmp_path, source_crs))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return ProjectUploadJobOut(job_id=job_id)


@router.get("/upload/{job_id}", response_model=ProjectUploadJobStatus)
async def upload_status(job_id: str) -> ProjectUploadJobStatus:
    # No project lookup here on purpose, same reasoning as
    # routes_generate.py's generate_status: polled every ~second for however
    # long parsing takes, and job_id is an unguessable random id that already
    # fully identifies the job.
    job = project_jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задача загрузки не найдена — возможно, сервер перезапускался.")
    return ProjectUploadJobStatus(status=job.status, project_id=job.project_id, error=job.error)


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(project: ProjectMetaDep) -> ProjectOut:
    return ProjectOut(
        id=project.id,
        name=project.name,
        source_crs=project.source_crs,
        crs_verified=project.crs_verified,
        created_at=project.created_at,
    )


@router.get("/{project_id}/layers", response_model=GeoJSONFeatureCollection)
async def get_project_layers(project: ProjectDep) -> GeoJSONFeatureCollection:
    """The full vector geometry ProjectOut used to carry inline -- now its
    own lazy endpoint, fetched only by the 3D view (see ProjectOut's own
    docstring for why: on a real-scale project this is a genuinely slow,
    genuinely large response, and most sessions never open 3D at all)."""
    return layers_to_feature_collection(project.layers, display_crs(project))


@router.get("/{project_id}/layers-raster", response_model=LayersRasterOut)
async def get_layers_raster(project: ProjectMetaDep) -> LayersRasterOut:
    """Grouping/bounds/counts for the map's source-layer backdrop -- see
    layer_raster.py's docstring for why the layers themselves are pixels, not
    GeoJSON features, by the time they reach the map."""
    raster = await get_layer_raster(project)
    return LayersRasterOut(
        bounds=raster.bounds,
        groups=[LayersRasterGroupOut(key=g.key, label=g.label, color=g.color, count=g.count) for g in raster.groups],
    )


@router.get("/{project_id}/layers-raster/image")
async def get_layers_raster_image(project: ProjectMetaDep, group: str) -> Response:
    raster = await get_layer_raster(project)
    for candidate in raster.groups:
        if candidate.key == group:
            return Response(content=candidate.png, media_type="image/png")
    raise HTTPException(status_code=404, detail=f"Группа слоёв «{group}» не найдена.")
