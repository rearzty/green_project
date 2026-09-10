from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from backend.app.schemas.geo import GeoJSONFeatureCollection


class ProjectCreateResponse(BaseModel):
    project_id: str


class ProjectOut(BaseModel):
    id: str
    name: str
    source_crs: str | None
    created_at: datetime
    layers: GeoJSONFeatureCollection
