from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile

from backend.app.api.deps import ProjectDep, ProjectMetaDep
from backend.app.schemas.geo import GeoJSONFeatureCollection
from backend.app.schemas.project import LayersRasterGroupOut, LayersRasterOut, ProjectCreateResponse, ProjectOut
from backend.app.services.geo_io import display_crs, layers_to_feature_collection
from backend.app.services.layer_raster import get_layer_raster
from backend.app.services.project_service import UnsupportedFileTypeError, create_project_from_file

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.post("", response_model=ProjectCreateResponse)
async def upload_project(
    name: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
    source_crs: Annotated[str | None, Form()] = None,
) -> ProjectCreateResponse:
    suffix = Path(file.filename or "").suffix.lower()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)

    try:
        project = await create_project_from_file(name=name, upload_path=tmp_path, source_crs=source_crs)
    except UnsupportedFileTypeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        tmp_path.unlink(missing_ok=True)

    return ProjectCreateResponse(project_id=project.id)


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(project: ProjectMetaDep) -> ProjectOut:
    return ProjectOut(
        id=project.id,
        name=project.name,
        source_crs=project.source_crs,
        crs_verified=project.crs_verified,
        created_at=project.created_at,
    )


@router.get("/{project_id}/layers", response_model=GeoJSONFeatureCollection)
async def get_project_layers(project: ProjectDep) -> GeoJSONFeatureCollection:
    """The full vector geometry ProjectOut used to carry inline -- now its
    own lazy endpoint, fetched only by the 3D view (see ProjectOut's own
    docstring for why: on a real-scale project this is a genuinely slow,
    genuinely large response, and most sessions never open 3D at all)."""
    return layers_to_feature_collection(project.layers, display_crs(project))


@router.get("/{project_id}/layers-raster", response_model=LayersRasterOut)
async def get_layers_raster(project: ProjectMetaDep) -> LayersRasterOut:
    """Grouping/bounds/counts for the map's source-layer backdrop -- see
    layer_raster.py's docstring for why the layers themselves are pixels, not
    GeoJSON features, by the time they reach the map."""
    raster = await get_layer_raster(project)
    return LayersRasterOut(
        bounds=raster.bounds,
        groups=[LayersRasterGroupOut(key=g.key, label=g.label, color=g.color, count=g.count) for g in raster.groups],
    )


@router.get("/{project_id}/layers-raster/image")
async def get_layers_raster_image(project: ProjectMetaDep, group: str) -> Response:
    raster = await get_layer_raster(project)
    for candidate in raster.groups:
        if candidate.key == group:
            return Response(content=candidate.png, media_type="image/png")
    raise HTTPException(status_code=404, detail=f"Группа слоёв «{group}» не найдена.")
