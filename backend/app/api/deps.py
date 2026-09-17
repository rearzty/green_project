from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException

from backend.app.db import session as store
from backend.app.db.models import Plan, PlantingItemRow, Project
from backend.app.services.pipeline_service import ensure_materialized


def get_project_or_404(project_id: str) -> Project:
    project = store.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Проект не найден — возможно, он был удалён.")
    return project


ProjectDep = Annotated[Project, Depends(get_project_or_404)]
# No database round trip left to avoid -- both names used to point at
# different dependencies (one eager-loading project.layers via SQL, one
# not) purely to dodge an expensive selectinload on the routes that didn't
# need it. Everything is already an in-memory object now, so there's nothing
# left to be lean about; kept as a second name only so routes_projects.py
# doesn't need editing just for this.
ProjectMetaDep = ProjectDep


async def get_plan_or_404(project_id: str, plan_id: str) -> Plan:
    project = get_project_or_404(project_id)
    plan = next((p for p in project.plans if p.id == plan_id), None)
    if plan is None:
        raise HTTPException(status_code=404, detail="План не найден в этом проекте.")
    if not plan.materialized:
        # A plan not currently open and never hand-edited has its items
        # pruned (see pipeline_service.Plan docstring) -- recompute them
        # transparently the moment anything asks for this plan (view it, edit
        # it, validate it, export it), rather than making every route that can
        # receive an old plan_id handle "materialized or not" itself.
        plan = await ensure_materialized(plan)
    return plan


PlanDep = Annotated[Plan, Depends(get_plan_or_404)]


def get_item_or_404(plan: Plan, item_id: str) -> PlantingItemRow:
    for item in plan.items:
        if item.id == item_id:
            return item
    raise HTTPException(status_code=404, detail="Объект плана не найден — возможно, он уже удалён.")
