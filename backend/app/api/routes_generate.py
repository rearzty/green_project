from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from backend.app.api.deps import PlanDep, ProjectDep, SessionDep
from backend.app.db.models import Plan
from backend.app.schemas.plan import GenerateRequest, PlanOut, PlanSummary
from backend.app.services.geo_io import planting_items_to_feature_collection
from backend.app.services.pipeline_service import generate_plan

router = APIRouter(prefix="/api/projects/{project_id}", tags=["plans"])


def _to_plan_out(plan: Plan) -> PlanOut:
    return PlanOut(
        plan_id=plan.id,
        scoring_mode=plan.scoring_mode,
        features=planting_items_to_feature_collection(plan.items, plan.project.source_crs),
    )


@router.post("/generate", response_model=PlanOut)
async def generate(request: GenerateRequest, project: ProjectDep, session: SessionDep) -> PlanOut:
    plan = await generate_plan(session, project, planting_types=request.planting_types, scoring_mode=request.scoring_mode)
    return _to_plan_out(plan)


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
    return _to_plan_out(plan)
