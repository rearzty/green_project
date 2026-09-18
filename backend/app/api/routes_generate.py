from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

from backend.app.api.deps import PlanDep, ProjectDep, get_project_or_404
from backend.app.db.models import Plan
from backend.app.schemas.plan import GenerateJobOut, GenerateJobStatus, GenerateRequest, PlanOut, PlanSummary
from backend.app.services import generation_jobs
from backend.app.services.geo_io import planting_items_to_feature_collection
from backend.app.services.pipeline_service import CurrentPlanDeletionError, MissingTerritoryError, delete_plan, generate_plan
from geo_engine.candidates import TooManyCandidatesError

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
    """Runs on the event loop after POST /generate has already returned."""
    try:
        project = get_project_or_404(project_id)
        plan = await generate_plan(
            project,
            planting_types=planting_types,
            scoring_mode=scoring_mode,
            tree_spacing_m=tree_spacing_m,
            shrub_spacing_m=shrub_spacing_m,
        )
        generation_jobs.mark_done(job_id, plan.id)
    except (MissingTerritoryError, TooManyCandidatesError) as exc:
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
    # unguessable random id that already fully identifies the job.
    job = generation_jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задача генерации не найдена — возможно, сервер перезапускался.")
    return GenerateJobStatus(status=job.status, plan_id=job.plan_id, error=job.error)


@router.get("/plans", response_model=list[PlanSummary])
async def list_plans(project: ProjectDep) -> list[PlanSummary]:
    """Every past generation for this project (heuristic and ml alike), not
    just whichever one was generated most recently. Reads Plan.item_count
    (a snapshot written by generate_plan/edit_service) rather than counting
    live items, most plans in the list have had those pruned back to a
    recipe (see pipeline_service.Plan docstring).
    """
    return [
        PlanSummary(plan_id=plan.id, scoring_mode=plan.scoring_mode, created_at=plan.created_at, is_current=plan.is_current, item_count=plan.item_count)
        for plan in sorted(project.plans, key=lambda p: p.created_at, reverse=True)
    ]


@router.get("/plans/{plan_id}", response_model=PlanOut)
async def get_plan(plan: PlanDep) -> PlanOut:
    # Off the event loop: building potentially hundreds of thousands of
    # GeoJSON features (even vectorized -- see planting_items_to_feature_collection)
    # is real CPU work that would otherwise stall every other request meanwhile.
    return await run_in_threadpool(_to_plan_out, plan)


@router.delete("/plans/{plan_id}", status_code=204)
async def delete_plan_route(project_id: str, plan_id: str) -> None:
    """Removes one plan from history permanently -- no undo, unlike the
    item-level edits in routes_edit.py. Doesn't use PlanDep/get_plan_or_404
    on purpose: that dependency transparently rematerializes a pruned plan
    (ensure_materialized) before handing it back, which would mean
    recomputing potentially hundreds of thousands of rows just to delete them
    a moment later -- a plain lookup is all a delete needs."""
    project = get_project_or_404(project_id)
    plan = next((p for p in project.plans if p.id == plan_id), None)
    if plan is None:
        raise HTTPException(status_code=404, detail="План не найден в этом проекте.")
    try:
        delete_plan(project, plan)
    except CurrentPlanDeletionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
