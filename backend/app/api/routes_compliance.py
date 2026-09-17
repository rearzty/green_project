"""Per-item and whole-plan normative justification -- the "why here, under
which act" the brief makes a hard requirement (see geo_engine.compliance's
own docstring). Three shapes of the same underlying check:

* POST .../compliance/items -- a handful of ids (what the map's "why here?"
  panel asks about when exactly one item is selected). Cheap and synchronous.
* GET .../compliance-report.json / .../compliance-report.csv -- the whole
  plan, as a download. Also synchronous for now: explain_items is one
  vectorized STRtree pass per constraint type, not the multi-minute DXF-export
  or plan-generation work that earned those a background job (see
  routes_export.py/routes_generate.py) -- revisit if a real-scale plan proves
  this wrong, same as those two did.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse

from backend.app.api.deps import PlanDep
from backend.app.core.config import settings
from backend.app.db.models import Plan
from backend.app.schemas.compliance import ComplianceCheckOut, ItemComplianceOut, ItemsComplianceRequest, ItemsComplianceResponse
from backend.app.services import compliance_service
from backend.app.services.pipeline_service import _effective_norms
from geo_engine.norms import PlantingNorms, load_norms

router = APIRouter(prefix="/api/projects/{project_id}/plans/{plan_id}", tags=["compliance"])


def _norms_for(plan: Plan) -> PlantingNorms:
    # Same per-plan spacing override generate_plan/ensure_materialized apply
    # (backend/app/services/pipeline_service.py) -- setbacks_m itself doesn't
    # depend on tree/shrub spacing today, but computing it any other way here
    # would be a second place to forget updating if that ever changes.
    return _effective_norms(load_norms(settings.planting_norms_path), plan.tree_spacing_m, plan.shrub_spacing_m)


def _to_schema(item_id: str, record) -> ItemComplianceOut:
    return ItemComplianceOut(
        item_id=item_id,
        planting_type=record.planting_type,
        species=record.species,
        compliant=record.compliant,
        binding_constraint=record.binding_constraint,
        summary=record.summary,
        checks=[
            ComplianceCheckOut(
                object_type=c.object_type,
                required_m=c.required_m,
                actual_m=c.actual_m,
                satisfied=c.satisfied,
                citation=c.citation,
                table_row=c.table_row,
                verified=c.verified,
            )
            for c in record.checks
        ],
    )


@router.post("/compliance/items", response_model=ItemsComplianceResponse)
async def compliance_for_items(request: ItemsComplianceRequest, plan: PlanDep) -> ItemsComplianceResponse:
    """The map's "why here?" panel -- explanation for exactly the items asked
    about (in practice: the one currently selected item), not the whole plan."""
    norms = _norms_for(plan)
    pairs = await run_in_threadpool(compliance_service.explain_items_by_id, plan.project, plan, request.ids, norms)
    return ItemsComplianceResponse(items=[_to_schema(item_id, record) for item_id, record in pairs])


@router.get("/compliance-report.json")
async def compliance_report_json(plan: PlanDep) -> JSONResponse:
    norms = _norms_for(plan)
    report = await run_in_threadpool(
        compliance_service.full_report,
        plan.project,
        plan,
        norms,
        project_id=plan.project_id,
        plan_id=plan.id,
    )
    return JSONResponse(content=report)


@router.get("/compliance-report.csv")
async def compliance_report_csv(plan: PlanDep, background_tasks: BackgroundTasks) -> FileResponse:
    norms = _norms_for(plan)
    handle = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
    handle.close()
    path = Path(handle.name)
    await run_in_threadpool(compliance_service.full_trace_csv, plan.project, plan, norms, path)
    # Same reasoning as routes_export.py's download route: unlink only after
    # FileResponse has finished streaming, via BackgroundTasks, not inline.
    background_tasks.add_task(path.unlink, missing_ok=True)
    return FileResponse(path=path, media_type="text/csv", filename=f"compliance_{plan.id}.csv")
