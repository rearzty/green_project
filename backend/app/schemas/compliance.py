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
