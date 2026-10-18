from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

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


class AssistantSelection(BaseModel):
    """What is selected on the map right now — Юна can act on it ("удали
    выделенные", "сделай их деревьями") and must know it is there."""

    count: int = 0
    trees: int = 0
    shrubs: int = 0
    lawns: int = 0


class AssistantMessageRequest(BaseModel):
    message: str
    history: list[AssistantChatMessage] = []
    settings: AssistantGenerationSettings
    # Какой план на экране и какая посадка выделена — чтобы Юна
    # отвечала по фактам плана («почему мало деревьев», «почему здесь»), а
    # не общими словами. Оба необязательны: без плана она просто настраивает
    # генерацию, как раньше.
    plan_id: str | None = None
    selected_item_id: str | None = None
    selection: AssistantSelection | None = None
    # None — план ещё не проверен на нормативы.
    violations_count: int | None = None
    view: Literal["2d", "3d"] = "2d"


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


class SettingsCommand(BaseModel):
    """Перегенерировать план с новым рецептом (тот же путь, что кнопка панели)."""

    type: Literal["settings"] = "settings"
    settings: AssistantAction


class EditSelectionCommand(BaseModel):
    """Правка того, что выделено на карте, — через те же батч-правки по id,
    что кнопки карточки, с отменой (Ctrl+Z)."""

    type: Literal["edit_selection"] = "edit_selection"
    operation: Literal["delete", "to_tree", "to_shrub"]


class SelectCommand(BaseModel):
    """Выделить на карте группу посадок (и показать первую)."""

    type: Literal["select"] = "select"
    target: Literal["violations", "trees", "shrubs", "lawn", "all", "none"]
    # Необязательный фильтр по породе для trees/shrubs (как в плане, напр. «Липа мелколистная»).
    species: str | None = None


class UiCommand(BaseModel):
    """Действие интерфейса: экспорт, отчёт обоснований, переключение 2D/3D."""

    type: Literal["ui"] = "ui"
    command: Literal["export_dxf", "report_json", "report_csv", "show_3d", "show_2d"]


AssistantCommand = Annotated[
    Union[SettingsCommand, EditSelectionCommand, SelectCommand, UiCommand], Field(discriminator="type")
]


class AssistantMessageResponse(BaseModel):
    reply: str
    # Действия по порядку; фронт выполняет их тем же кодом, что кнопки.
    actions: list[AssistantCommand] = []
    # Устаревшее поле для совместимости: рецепт первой SettingsCommand.
    action: AssistantAction | None = None
    # Кто ответил: языковая модель или встроенный разбор команд (без ИИ:
    # ключа нет, сеть недоступна, исчерпан лимит).
    mode: Literal["llm", "rules"] = "llm"
    # Пояснение для пользователя, почему ответ в базовом режиме, и т. п.
    notice: str | None = None


class AssistantStatus(BaseModel):
    """GET /api/config/assistant — подключена ли языковая модель."""

    llm_configured: bool
    provider: str
    model: str | None = None
