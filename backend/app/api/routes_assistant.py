from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

from backend.app.api.deps import ProjectDep
from backend.app.schemas.assistant import AssistantMessageRequest, AssistantMessageResponse
from backend.app.services.assistant_context import plan_context
from backend.app.services.assistant_service import AssistantNotConfiguredError, AssistantUpstreamError, ask_yuna

# Nested under /api/projects/{project_id}: when the request names a plan_id,
# Юна gets a compact summary of that plan (counts, schemes, area budget) and,
# for one selected item, its normative justification -- see
# assistant_context.py. ProjectDep also gives a proper 404 for a stale project
# id instead of a confusing LLM error.
router = APIRouter(prefix="/api/projects/{project_id}", tags=["assistant"])


@router.post("/assistant/message", response_model=AssistantMessageResponse)
async def send_message(request: AssistantMessageRequest, project: ProjectDep) -> AssistantMessageResponse:
    context = None
    if request.plan_id:
        context = await run_in_threadpool(plan_context, project, request.plan_id, request.selected_item_id)
    try:
        reply, action = await ask_yuna(request.message, request.history, request.settings, context)
    except AssistantNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AssistantUpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return AssistantMessageResponse(reply=reply, action=action)
