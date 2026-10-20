"""Response shapes for the per-item normative justification endpoints.

Deliberately not a thin wrapper around geo_engine.compliance's dataclasses:
those carry a positional `index`, not a PlantingItemRow.id, and the point of
this schema is exactly that a map click needs to look an explanation up by the
id it already has (see compliance_service.explain_items_by_id).
"""

from __future__ import annotations

from pydantic import BaseModel


class ComplianceCheckOut(BaseModel):
    object_type: str
    required_m: float
    actual_m: float
    satisfied: bool
    citation: str
    table_row: str
    verified: bool
    # Подпись объекта по-русски и поправка по породе — чтобы карточка «Почему
    # здесь?» не держала свою копию OBJECT_LABELS_RU (geo_engine/compliance.py).
    object_label: str = ""
    species_rule: str = ""


class ItemComplianceOut(BaseModel):
    item_id: str
    planting_type: str
    species: str
    compliant: bool
    binding_constraint: str | None
    summary: str
    checks: list[ComplianceCheckOut]


class ItemsComplianceRequest(BaseModel):
    """Which items to explain -- mirrors ValidateItemsRequest's shape (see
    schemas/plan.py), same reasoning: a map click asks about what it actually
    selected, not the whole plan."""

    ids: list[str]


class ItemsComplianceResponse(BaseModel):
    items: list[ItemComplianceOut]


class SpotRequest(BaseModel):
    """Точка на карте (те же координаты, что у правок: WGS84 lng/lat или
    локальные метры, если СК проекта не проверена) и тип посадки, который в неё
    хотели поставить."""

    x: float
    y: float
    planting_type: str = "tree"
    species: str = ""


class RejectionReasonOut(BaseModel):
    kind: str
    text: str
    citation: str
    verified: bool
    object_type: str | None = None
    required_m: float | None = None
    actual_m: float | None = None


class SpotResponse(BaseModel):
    # Пустой список reasons — место допустимо.
    allowed: bool
    reasons: list[RejectionReasonOut]
