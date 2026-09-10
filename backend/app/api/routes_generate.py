from __future__ import annotations

from fastapi import APIRouter

from backend.app.api.deps import PlanDep, ProjectDep, SessionDep
from backend.app.db.models import Plan
from backend.app.schemas.plan import GenerateRequest, PlanOut
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
def generate(request: GenerateRequest, project: ProjectDep, session: SessionDep) -> PlanOut:
    plan = generate_plan(session, project, planting_types=request.planting_types, scoring_mode=request.scoring_mode)
    return _to_plan_out(plan)


@router.get("/plans/{plan_id}", response_model=PlanOut)
def get_plan(plan: PlanDep) -> PlanOut:
    return _to_plan_out(plan)
