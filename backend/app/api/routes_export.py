from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from backend.app.api.deps import get_plan_or_404
from backend.app.db.models import Plan
from backend.app.services.export_service import plan_to_dxf

router = APIRouter(prefix="/api/projects/{project_id}/plans/{plan_id}", tags=["export"])


@router.get("/export.dxf")
def export_dxf(plan: Plan = Depends(get_plan_or_404)) -> FileResponse:
    output_path = Path(tempfile.gettempdir()) / f"plan_{plan.id}.dxf"
    plan_to_dxf(plan, output_path)
    return FileResponse(
        path=output_path,
        media_type="application/dxf",
        filename=f"planting_plan_{plan.id}.dxf",
    )
