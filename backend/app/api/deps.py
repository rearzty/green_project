from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.db.models import Plan, PlantingItemRow, Project
from backend.app.db.session import get_session
from backend.app.services.pipeline_service import ensure_materialized

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_project_or_404(project_id: str, session: SessionDep) -> Project:
    # selectinload: project.layers/project.plans get touched later (domain
    # conversion, DXF export, generate_plan's "mark old plans not current")
    # outside this request's immediate async context in some call paths
    # (e.g. handed to run_in_threadpool) -- lazy-loading there would need an
    # await a plain thread can't make, so load both eagerly up front.
    result = await session.execute(
        select(Project).where(Project.id == project_id).options(selectinload(Project.layers), selectinload(Project.plans))
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Проект не найден — возможно, он был удалён.")
    return project


ProjectDep = Annotated[Project, Depends(get_project_or_404)]


async def get_plan_or_404(project_id: str, plan_id: str, session: SessionDep) -> Plan:
    result = await session.execute(
        select(Plan)
        .where(Plan.id == plan_id)
        .options(selectinload(Plan.items), selectinload(Plan.project).selectinload(Project.layers))
    )
    plan = result.scalar_one_or_none()
    if plan is None or plan.project_id != project_id:
        raise HTTPException(status_code=404, detail="План не найден в этом проекте.")
    if not plan.materialized:
        # A plan not currently open and never hand-edited has its
        # planting_items rows pruned (see pipeline_service.Plan docstring) --
        # recompute them transparently the moment anything asks for this
        # plan's items (view it, edit it, validate it, export it), rather
        # than making every route that can receive an old plan_id handle
        # "materialized or not" itself.
        plan = await ensure_materialized(session, plan)
    return plan


PlanDep = Annotated[Plan, Depends(get_plan_or_404)]


def get_item_or_404(plan: Plan, item_id: str) -> PlantingItemRow:
    for item in plan.items:
        if item.id == item_id:
            return item
    raise HTTPException(status_code=404, detail="Объект плана не найден — возможно, он уже удалён.")
