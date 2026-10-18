from __future__ import annotations

from fastapi import APIRouter

from backend.app.core.config import settings
from backend.app.schemas.assistant import AssistantStatus
from geo_engine.norms import load_norms

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("/planting-norms")
def get_planting_norms() -> dict:
    """Exposes the same rulebook geo_engine uses internally, so the frontend
    can render the legend and do client-side sanity checks without
    duplicating the setback values.
    """
    return load_norms(settings.planting_norms_path).model_dump()


@router.get("/assistant", response_model=AssistantStatus)
def get_assistant_status() -> AssistantStatus:
    """Whether Юна has a language model behind her or answers with the
    built-in parser -- lets the chat say "базовый режим" up front instead of
    the user discovering it from the answers. Never exposes the key."""
    configured = settings.llm_configured
    return AssistantStatus(
        llm_configured=configured,
        provider=settings.llm_provider,
        model=settings.llm_model if configured else None,
    )
