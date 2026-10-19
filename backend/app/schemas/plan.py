from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from backend.app.schemas.geo import GeoJSONFeature, GeoJSONFeatureCollection, GeoJSONGeometry

ScoringMode = Literal["heuristic", "ml"]
PlantingType = Literal["tree", "shrub", "lawn"]


class GenerateRequest(BaseModel):
    planting_types: list[PlantingType] = ["tree", "shrub", "lawn"]
    scoring_mode: ScoringMode = "heuristic"
    # None -- the far more common case -- means "use planting_norms.yaml's
    # own tree_default/shrub_default"; bounds are sanity rails (a few
    # centimeters would flood the plan with candidates the way an
    # accidentally-tiny canopy_radius_m once did, see CLAUDE.md), not a
    # claim about what's landscaping-correct.
    tree_spacing_m: float | None = Field(default=None, ge=0.5, le=15.0)
    shrub_spacing_m: float | None = Field(default=None, ge=0.3, le=10.0)
    # 2 — деревья вдоль улиц двухрядной аллеей (743-ПП табл. 3.6.2).
    tree_rows: int = Field(default=1, ge=1, le=2)
    # Деревья в покрытии тротуара с приствольной решёткой (743-ПП п. 9.8).
    trees_in_paving: bool = False
    # Согласие строить по границе заказа на съёмку, когда границы работ в
    # поставке нет (geo_engine.territory.OrderBoundaryOnlyError).
    use_order_boundary: bool = False


class GenerateJobOut(BaseModel):
    """POST /generate starts a background job instead of blocking the
    request for however long candidate generation + placement takes on a
    real-scale territory -- see backend/app/services/generation_jobs.py."""

    job_id: str


GenerateJobState = Literal["pending", "done", "error"]


class GenerateJobStatus(BaseModel):
    status: GenerateJobState
    plan_id: str | None = None
    error: str | None = None
    # Best-effort, same shape as ProjectUploadJobStatus -- see
    # generation_jobs.py's own docstring. `stage` names the planting_type
    # that most recently finished; `progress` is types-done/types-total.
    stage: str | None = None
    progress: float | None = None


class ExportJobOut(BaseModel):
    """POST /export-dxf starts a background job instead of blocking the
    request -- writing a real-scale plan (hundreds of thousands of items)
    measured up to ~3 minutes (see CLAUDE.md's export benchmark), the same
    class of problem /generate already solves this way. See
    backend/app/services/export_jobs.py."""

    job_id: str


ExportLayerKey = Literal["tree", "shrub", "lawn", "violations"]


class ExportOptionsIn(BaseModel):
    """Что уйдёт в файл (диалог «Перед экспортом»). Всё необязательное: пустое
    тело — прежний экспорт целиком."""

    # Слои результата; None — все. «violations» — кольца вокруг нарушений.
    layers: list[ExportLayerKey] | None = None
    # План поверх исходного чертежа (и файлы его внешних ссылок рядом). Без
    # подосновы план пишется отдельным чертежом.
    include_base: bool = True
    # plan.report.json и plan.trace.csv — обоснование каждой посадки.
    include_report: bool = True


class ExportLayerOut(BaseModel):
    key: ExportLayerKey
    # Имя слоя в DXF — ровно то, что пишет экспорт (geo_engine/io/dxf_writer.py).
    name: str
    count: int
    # Только у газона: площадь, м².
    area_m2: float | None = None


class UnverifiedCitationOut(BaseModel):
    citation: str
    items: int
    # Посадки, у которых решающее ограничение держится на этой ссылке (не
    # больше MAX_UNVERIFIED_ITEM_IDS) — чтобы показать их на карте.
    item_ids: list[str]


class ExportSummaryOut(BaseModel):
    """Сводка для диалога «Перед экспортом»: считается тем же `explain_items`,
    что пишет отчёт, — числа в диалоге и в скачанном файле не расходятся."""

    total_items: int
    violations: int
    unverified_citations: list[UnverifiedCitationOut]
    layers: list[ExportLayerOut]
    # Исходный чертёж доступен: план ляжет на подоснову в тех же координатах.
    base_available: bool
    # Сколько слоёв исходного чертежа прочиталось (None — неизвестно).
    base_layer_count: int | None = None


ExportJobState = Literal["pending", "done", "error"]


class ExportJobStatus(BaseModel):
    status: ExportJobState
    error: str | None = None


class AreaBlockerOut(BaseModel):
    object_type: str
    label: str
    share: float


class ZoneGroupOut(BaseModel):
    """Одна полоса слоя «где можно сажать»: допустимая площадь или зона,
    закрытая одним ограничением."""

    key: str
    kind: Literal["allowed", "blocker"]
    label: str
    color: str
    share: float
    required_m: float | None = None
    # Площадь, закрытая только этим ограничением, м² — освободится, если его убрать.
    exclusive_m2: float = 0.0


class ZonesAdviceOut(BaseModel):
    key: str
    label: str
    exclusive_m2: float
    extra_items: int


class PlanZonesOut(BaseModel):
    planting_type: PlantingType
    bounds: tuple[tuple[float, float], tuple[float, float]]
    territory_m2: float
    usable_m2: float
    usable_share: float
    interval_m: float
    groups: list[ZoneGroupOut]
    advice: ZonesAdviceOut | None = None


class AreaBudgetOut(BaseModel):
    """Сколько площади осталось под тип посадки и что закрыло остальное —
    см. geo_engine/area_budget.py."""

    planting_type: str
    territory_m2: float
    usable_m2: float
    usable_share: float
    blockers: list[AreaBlockerOut]


class PlanOut(BaseModel):
    plan_id: str
    scoring_mode: ScoringMode
    features: GeoJSONFeatureCollection
    area_budget: list[AreaBudgetOut] = []
    # Остальной рецепт плана — чтобы панель и Юна после перезагрузки страницы
    # или переключения версии показывали и меняли настройки ИМЕННО этого плана,
    # а не то, что осталось в полях от прошлого (иначе следующая правка
    # интервала молча теряла двухрядную аллею).
    planting_types: list[PlantingType] = []
    tree_spacing_m: float | None = None
    shrub_spacing_m: float | None = None
    tree_rows: int = 1
    trees_in_paving: bool = False


class PlanSummary(BaseModel):
    """One row in the project's plan history — lets the UI show every past
    generation (heuristic vs ml, one run vs another) instead of only ever
    tracking whichever plan was generated most recently, which is what made
    separate generations look like they were "getting mixed up" together.
    """

    plan_id: str
    scoring_mode: ScoringMode
    created_at: datetime
    is_current: bool
    item_count: int


class ItemPatch(BaseModel):
    """Graphical edit: move/retype/restyle one planting item. Omit a field to leave it unchanged."""

    geometry: GeoJSONGeometry | None = None
    planting_type: PlantingType | None = None
    species: str | None = None


class ItemIdsRequest(BaseModel):
    ids: list[str]


class ItemsDeleteResult(BaseModel):
    """Pre-delete snapshots, for undo via POST .../items/restore. `item_count`
    is the plan's fresh total -- lets the frontend update the plan-history
    sidebar's count for this plan without a separate GET .../plans call."""

    deleted_items: list[GeoJSONFeature]
    item_count: int


class ItemsRespaceRequest(BaseModel):
    """Перерасставить ряд с другим шагом (метры). Нижняя граница — не норматив
    (его проверяет сервис по породе), а защита от шага в сантиметры, который
    раздул бы ряд в тысячи точек."""

    ids: list[str]
    pitch_m: float = Field(ge=0.3, le=30.0)


class ItemsRespaceResult(BaseModel):
    """`previous_items` — снимки прежних посадок ряда (для отмены через
    delete+restore), `items` — новые; `item_count` — свежее число посадок плана."""

    previous_items: list[GeoJSONFeature]
    items: list[GeoJSONFeature]
    item_count: int


FixKind = Literal["move", "retype", "delete"]


class FixPointOut(BaseModel):
    """Точка в тех же координатах, что и остальная геометрия ответов (WGS84,
    если СК проекта подтверждена, иначе метры чертежа)."""

    x: float
    y: float


class FixOptionOut(BaseModel):
    """Один способ исправить нарушение — см. backend/app/services/fix_service.py.
    Поля вида (`to`/`shift_m`/…, `type`/`valid`/…, `gap_m`) заполнены только у
    своего `kind`."""

    kind: FixKind
    # Лучший по мнению сервиса; ставится только когда есть из чего выбирать.
    recommended: bool = False
    # move
    to: FixPointOut | None = None
    shift_m: float | None = None
    # Расстояние до нарушенного ограничения после сдвига, м.
    clearance_m: float | None = None
    # Остаётся ли посадка в линии своего ряда; None — она не из ряда.
    keeps_row: bool | None = None
    # retype
    type: PlantingType | None = None
    valid: bool | None = None
    required_m: float | None = None
    # delete: пропуск, который останется в ряду, м.
    gap_m: float | None = None


class ItemFixesOut(BaseModel):
    item_id: str
    options: list[FixOptionOut]


class FixesApplyRequest(BaseModel):
    """«Исправить все так же»: применить к этим посадкам лучший вариант вида `kind`."""

    ids: list[str] = Field(min_length=1, max_length=500)
    kind: FixKind


class FixesApplyResult(BaseModel):
    """`previous_items` — снимки прежних посадок (для отмены), `items` — сдвинутые
    и пересаженные (id прежние; удалённых среди них нет), `skipped_ids` — те, у
    кого нужного варианта нет; `item_count` — свежее число посадок плана."""

    previous_items: list[GeoJSONFeature]
    items: list[GeoJSONFeature]
    skipped_ids: list[str]
    item_count: int


class ItemTypeChange(BaseModel):
    id: str
    planting_type: PlantingType


class ItemsRetypeRequest(BaseModel):
    """One target type per item -- a selection sends the same type for all,
    an undo sends each item's own previous type back."""

    changes: list[ItemTypeChange]


class ItemsRetypeResult(BaseModel):
    """`previous_items`: pre-change snapshots of what actually changed (for
    undo) -- geometry never changes here, so the frontend patches its local
    copy's `planting_type` directly rather than needing a post-change
    snapshot back. `skipped_ids`: items whose geometry can't take the
    requested type (e.g. a lawn polygon asked to become a tree), left
    untouched."""

    previous_items: list[GeoJSONFeature]
    skipped_ids: list[str]


class LngLat(BaseModel):
    x: float
    y: float


class ItemsMoveRequest(BaseModel):
    """Translate these items by the vector from `from_point` to `to_point`
    (the drag's start and end, WGS84 lng/lat)."""

    ids: list[str]
    from_point: LngLat
    to_point: LngLat


class ItemsMoveResult(BaseModel):
    """Post-move snapshots of the moved items -- the exact stored position
    depends on a WGS84 round trip through the project's source_crs, which the
    frontend's own optimistic drag preview (plain WGS84 math) doesn't
    replicate exactly, so it takes these back to patch its local copy rather
    than assuming its preview already matches what got stored."""

    items: list[GeoJSONFeature]


class ItemsRestoreRequest(BaseModel):
    """Undo's counterpart to a delete: recreate these exact items (same id,
    geometry, type, species, score, rationale) -- a delete's own
    `deleted_items` sent right back.
    """

    items: list[GeoJSONFeature]


class ItemsRestoreResult(BaseModel):
    """`item_count`: the plan's fresh total, same reasoning as
    ItemsDeleteResult. Nothing else -- the caller already has the exact
    features it just asked to restore."""

    item_count: int


class ValidationViolation(BaseModel):
    item_id: str
    message: str


class ValidateResponse(BaseModel):
    violations: list[ValidationViolation]
    # Только у /validate/items: чьи вердикты пересчитаны — тронутые плюс
    # соседние деревья, у которых мог смениться ближайший сосед. Фронт
    # заменяет подсветку именно у них, а не только у тронутых.
    checked_ids: list[str] | None = None


class ValidateItemsRequest(BaseModel):
    """Recheck only these items against the setback rulebook -- what an edit
    actually touched -- instead of the whole plan (see
    edit_service.validate_items)."""

    ids: list[str]
