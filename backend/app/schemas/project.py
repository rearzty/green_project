from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class ProjectCreateResponse(BaseModel):
    project_id: str


ProjectUploadJobState = Literal["pending", "done", "error"]


class ProjectUploadJobOut(BaseModel):
    """POST /api/projects starts a background job instead of blocking the
    request for however long DWG conversion + bundle parsing takes -- see
    backend/app/services/project_jobs.py."""

    job_id: str


class ProjectUploadJobStatus(BaseModel):
    status: ProjectUploadJobState
    project_id: str | None = None
    error: str | None = None


class ProjectOut(BaseModel):
    """Metadata only -- no `layers` field. The 2D map gets the source
    drawing from /layers-raster now (see layer_raster.py); the full vector
    GeoJSON this used to carry unconditionally is still available at
    GET /{project_id}/layers, fetched lazily by whatever still needs real
    geometry (currently only the 3D view, for its building extrusion) rather
    than on every project open. Measured why this split matters: on a real
    371,685-layer project, serializing that field alone made this endpoint a
    170MB response taking 60+ seconds -- paid on every restore/upload
    regardless of whether the viewer ever opened 3D."""

    id: str
    name: str
    source_crs: str | None
    # See Project.crs_verified's own docstring -- the map uses this to
    # decide whether a real OpenStreetMap basemap under the plan is honest.
    crs_verified: bool
    created_at: datetime


class LayersRasterGroupOut(BaseModel):
    key: str
    label: str
    color: str
    count: int


class LayersRasterOut(BaseModel):
    # ((south, west), (north, east)) -- same shape react-leaflet's
    # <ImageOverlay bounds=.../> takes directly. None for a project with no
    # layers at all.
    bounds: tuple[tuple[float, float], tuple[float, float]] | None
    groups: list[LayersRasterGroupOut]
