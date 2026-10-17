from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from backend.app.schemas.plan import PlantingType, ScoringMode


class AssistantChatMessage(BaseModel):
    """One turn of a chat the frontend already holds in memory (see
    AssistantChat.tsx) -- the backend is a stateless proxy to the LLM, it
    never stores conversation history itself, so the full transcript is sent
    back on every message."""

    role: Literal["user", "assistant"]
    content: str


class AssistantGenerationSettings(BaseModel):
    """The generation recipe as ControlPanel's own controlled state already
    holds it -- sent on every message so Юна reasons about NEW absolute
    values ("increase the tree interval") relative to what's actually set,
    rather than guessing a delta blind to the current number."""

    planting_types: list[PlantingType]
    tree_spacing_m: float | None = None
    shrub_spacing_m: float | None = None
    scoring_mode: ScoringMode = "heuristic"
    tree_rows: int = 1
    trees_in_paving: bool = False


class AssistantMessageRequest(BaseModel):
    message: str
    history: list[AssistantChatMessage] = []
    settings: AssistantGenerationSettings
    # Какой план сейчас на экране и какая посадка выделена — чтобы Юна
    # отвечала по фактам плана («почему мало деревьев», «почему здесь»), а
    # не общими словами. Оба необязательны: без плана она просто настраивает
    # генерацию, как раньше.
    plan_id: str | None = None
    selected_item_id: str | None = None


class AssistantAction(BaseModel):
    """What Юна decided to change, if anything. The frontend applies this by
    calling the exact same /generate flow the panel's own "Сгенерировать
    план" button already uses (lib/api.ts::generatePlan) -- background job,
    polling, plan-history refresh all come for free, nothing new to build on
    the execution side."""

    planting_types: list[PlantingType]
    tree_spacing_m: float | None = None
    shrub_spacing_m: float | None = None
    scoring_mode: ScoringMode = "heuristic"
    tree_rows: int = 1
    trees_in_paving: bool = False


class AssistantMessageResponse(BaseModel):
    reply: str
    action: AssistantAction | None = None
