from __future__ import annotations

from pathlib import Path

from backend.app.db.models import Plan
from backend.app.services.geo_io import db_to_shape
from geo_engine.io.dxf_writer import write_dxf
from geo_engine.model import PlantingItem


def plan_to_dxf(plan: Plan, output_path: str | Path) -> Path:
    items = [
        PlantingItem(
            geometry=db_to_shape(row.geometry),
            planting_type=row.planting_type,
            species=row.species,
            score=row.score,
            rationale=row.rationale,
            is_manual_edit=row.is_manual_edit,
        )
        for row in plan.items
    ]
    output_path = Path(output_path)
    write_dxf(items, output_path)
    return output_path
