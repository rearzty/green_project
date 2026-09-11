from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from backend.app.api.deps import PlanDep
from backend.app.services.export_service import plan_to_dxf

router = APIRouter(prefix="/api/projects/{project_id}/plans/{plan_id}", tags=["export"])


@router.get("/export.dxf")
async def export_dxf(plan: PlanDep, background_tasks: BackgroundTasks) -> FileResponse:
    output_path = Path(tempfile.gettempdir()) / f"plan_{plan.id}.dxf"
    await run_in_threadpool(plan_to_dxf, plan, output_path)
    background_tasks.add_task(output_path.unlink, missing_ok=True)
    return FileResponse(
        path=output_path,
        media_type="application/dxf",
        filename=f"planting_plan_{plan.id}.dxf",
    )
