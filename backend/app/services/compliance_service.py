"""Normative justification for a plan's items, exposed over the API.

geo_engine.compliance.explain_items() already does the real work (per-item
distance to every setback-relevant object type, compared against
planting_norms.yaml, cited by act/clause) -- this module is the thin
ORM-row <-> domain-object bridge PlanDep's already-loaded Plan/Project need to
call it, mirroring edit_service.validate_items's own "filter plan.items by id,
run the pure geo_engine check, return plain dicts" shape.

Deliberately does not touch geo_engine.compliance.ComplianceRecord.index (a
plain position in whatever list was passed in) -- the frontend needs to match
a record back to the PlantingItemRow.id it asked about, not a position, so
this module zips (row.id, record) itself rather than pushing id-awareness into
geo_engine, which has no id concept at all (it's PlantingItem, not
PlantingItemRow).
"""

from __future__ import annotations

from backend.app.db.models import Plan, PlantingItemRow, Project
from backend.app.services.geo_io import db_to_shape, layers_to_domain
from geo_engine.compliance import ComplianceRecord, explain_items, report_payload, write_trace_csv
from geo_engine.model import PlantingItem
from geo_engine.norms import PlantingNorms


def _row_to_domain(row: PlantingItemRow) -> PlantingItem:
    return PlantingItem(
        geometry=db_to_shape(row.geometry),
        planting_type=row.planting_type,
        species=row.species,
        score=row.score,
        rationale=row.rationale,
        is_manual_edit=row.is_manual_edit,
    )


def explain_rows(
    project: Project, rows: list[PlantingItemRow], norms: PlantingNorms
) -> list[tuple[str, ComplianceRecord]]:
    """(row.id, its ComplianceRecord), in the same order as `rows`.

    A record's distance checks only ever compare against the project's fixed
    utilities/zones, never against other plantings, so this works identically
    whether `rows` is a whole plan or the handful of ids one map click
    selected -- same reason edit_service.validate_items can check a subset.
    """
    if not rows:
        return []
    utilities, zones = layers_to_domain(project.layers)
    items = [_row_to_domain(row) for row in rows]
    records = explain_items(items, utilities, zones, norms)
    return list(zip((row.id for row in rows), records))


def explain_items_by_id(
    project: Project, plan: Plan, ids: list[str], norms: PlantingNorms
) -> list[tuple[str, ComplianceRecord]]:
    """Restricted to specific items -- what the map actually asked about."""
    wanted = set(ids)
    rows = [item for item in plan.items if item.id in wanted]
    return explain_rows(project, rows, norms)


def full_report(project: Project, plan: Plan, norms: PlantingNorms, **meta) -> dict:
    """The whole plan's justification, in the same normalized shape
    scripts/plan_dxf.py writes to <output>.report.json -- one artifact, CLI
    and web agree on its shape."""
    pairs = explain_rows(project, list(plan.items), norms)
    records = [record for _, record in pairs]
    return report_payload(records, norms, **meta)


def full_trace_csv(project: Project, plan: Plan, norms: PlantingNorms, path) -> int:
    """Same CSV scripts/plan_dxf.py's --csv writes, for the download route.
    Returns the row count written."""
    pairs = explain_rows(project, list(plan.items), norms)
    records = [record for _, record in pairs]
    return write_trace_csv(records, path)
