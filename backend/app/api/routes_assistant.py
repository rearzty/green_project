from __future__ import annotations

from fastapi import APIRouter, HTTPException

from backend.app.api.deps import ProjectDep
from backend.app.schemas.assistant import AssistantMessageRequest, AssistantMessageResponse
from backend.app.services.assistant_service import AssistantNotConfiguredError, AssistantUpstreamError, ask_yuna

# Nested under /api/projects/{project_id} for consistency with every other
# router even though Юна reads no project data today (she only reasons about
# the generation settings the frontend already sends) -- ProjectDep still
# gives a proper 404 for a stale/deleted project id instead of a confusing
# LLM error, and keeps the door open for project-specific context later
# without a URL shape change.
router = APIRouter(prefix="/api/projects/{project_id}", tags=["assistant"])


@router.post("/assistant/message", response_model=AssistantMessageResponse)
async def send_message(request: AssistantMessageRequest, _project: ProjectDep) -> AssistantMessageResponse:
    try:
        reply, action = await ask_yuna(request.message, request.history, request.settings)
    except AssistantNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AssistantUpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return AssistantMessageResponse(reply=reply, action=action)
