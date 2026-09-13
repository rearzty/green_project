from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select

from backend.app.api.deps import PlanDep, ProjectDep, SessionDep, get_project_or_404
from backend.app.db.models import Plan
from backend.app.db.session import SessionLocal
from backend.app.schemas.plan import GenerateJobOut, GenerateJobStatus, GenerateRequest, PlanOut, PlanSummary
from backend.app.services import generation_jobs
from backend.app.services.geo_io import planting_items_to_feature_collection
from backend.app.services.pipeline_service import MissingTerritoryError, generate_plan

router = APIRouter(prefix="/api/projects/{project_id}", tags=["plans"])
logger = logging.getLogger(__name__)

# Keeps a strong reference to in-flight background generate tasks -- without
# this, nothing else in the process holds one, and asyncio only guarantees a
# task survives if something does (a task with no live reference can be
# garbage-collected mid-run, silently abandoning the job).
_background_tasks: set[asyncio.Task] = set()


def _to_plan_out(plan: Plan) -> PlanOut:
    return PlanOut(
        plan_id=plan.id,
        scoring_mode=plan.scoring_mode,
        features=planting_items_to_feature_collection(plan.items, plan.project.source_crs),
    )


async def _run_generate_job(
    job_id: str,
    project_id: str,
    planting_types: list[str],
    scoring_mode: str,
    tree_spacing_m: float | None,
    shrub_spacing_m: float | None,
) -> None:
    """Runs on the event loop after POST /generate has already returned --
    the request's own DB session is closed by then, so this opens a fresh one
    rather than reusing it."""
    async with SessionLocal() as session:
        try:
            project = await get_project_or_404(project_id, session)
            plan = await generate_plan(
                session,
                project,
                planting_types=planting_types,
                scoring_mode=scoring_mode,
                tree_spacing_m=tree_spacing_m,
                shrub_spacing_m=shrub_spacing_m,
            )
            generation_jobs.mark_done(job_id, plan.id)
        except MissingTerritoryError as exc:
            generation_jobs.mark_error(job_id, str(exc))
        except HTTPException as exc:
            generation_jobs.mark_error(job_id, str(exc.detail))
        except Exception:
            logger.exception("Background plan generation failed (job_id=%s, project_id=%s)", job_id, project_id)
            generation_jobs.mark_error(job_id, "Не удалось сгенерировать план — внутренняя ошибка сервера.")


@router.post("/generate", response_model=GenerateJobOut, status_code=202)
async def generate(request: GenerateRequest, project: ProjectDep) -> GenerateJobOut:
    """Starts plan generation as a background job instead of blocking this
    request -- candidate generation + greedy placement can take tens of
    seconds on a real-scale territory (see CLAUDE.md). The frontend polls
    GET .../generate/{job_id} for completion, then fetches the plan through
    the existing GET .../plans/{plan_id}."""
    job_id = generation_jobs.create_job()
    task = asyncio.create_task(
        _run_generate_job(
            job_id, project.id, request.planting_types, request.scoring_mode, request.tree_spacing_m, request.shrub_spacing_m
        )
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return GenerateJobOut(job_id=job_id)


@router.get("/generate/{job_id}", response_model=GenerateJobStatus)
async def generate_status(project_id: str, job_id: str) -> GenerateJobStatus:
    # No project lookup here on purpose -- this route gets polled every
    # ~second for however long generation takes, and job_id is an
    # unguessable random id that already fully identifies the job without
    # needing to re-load (and re-selectinload) the project on every poll.
    job = generation_jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задача генерации не найдена — возможно, сервер перезапускался.")
    return GenerateJobStatus(status=job.status, plan_id=job.plan_id, error=job.error)


@router.get("/plans", response_model=list[PlanSummary])
async def list_plans(project: ProjectDep, session: SessionDep) -> list[PlanSummary]:
    """Every past generation for this project (heuristic and ml alike), not
    just whichever one was generated most recently. Reads Plan.item_count
    (a snapshot written by generate_plan/edit_service) rather than counting
    live planting_items rows -- most plans in the list have had those rows
    pruned back to a recipe (see pipeline_service.Plan docstring), so a
    COUNT/join here would undercount everything but the current plan.
    """
    result = await session.execute(select(Plan).where(Plan.project_id == project.id).order_by(Plan.created_at.desc()))
    return [
        PlanSummary(plan_id=plan.id, scoring_mode=plan.scoring_mode, created_at=plan.created_at, is_current=plan.is_current, item_count=plan.item_count)
        for plan in result.scalars()
    ]


@router.get("/plans/{plan_id}", response_model=PlanOut)
async def get_plan(plan: PlanDep) -> PlanOut:
    # Off the event loop: building potentially hundreds of thousands of
    # GeoJSON features (even vectorized -- see planting_items_to_feature_collection)
    # is real CPU work that would otherwise stall every other request meanwhile.
    return await run_in_threadpool(_to_plan_out, plan)
