from __future__ import annotations

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from backend.app.db.models import Plan, PlantingItemRow, Project
from backend.app.db.session import get_session


def get_project_or_404(project_id: str, session: Session = Depends(get_session)) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    return project


def get_plan_or_404(project_id: str, plan_id: str, session: Session = Depends(get_session)) -> Plan:
    plan = session.get(Plan, plan_id)
    if plan is None or plan.project_id != project_id:
        raise HTTPException(status_code=404, detail=f"Plan {plan_id} not found for project {project_id}")
    return plan


def get_item_or_404(plan: Plan, item_id: str) -> PlantingItemRow:
    for item in plan.items:
        if item.id == item_id:
            return item
    raise HTTPException(status_code=404, detail=f"Planting item {item_id} not found in plan {plan.id}")
