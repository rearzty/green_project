from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from backend.app.api.deps import ProjectDep, SessionDep
from backend.app.schemas.project import ProjectCreateResponse, ProjectOut
from backend.app.services.geo_io import layers_to_feature_collection
from backend.app.services.project_service import UnsupportedFileTypeError, create_project_from_file

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.post("", response_model=ProjectCreateResponse)
def upload_project(
    name: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
    session: SessionDep,
    source_crs: Annotated[str | None, Form()] = None,
) -> ProjectCreateResponse:
    suffix = Path(file.filename or "").suffix.lower()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file.file.read())
        tmp_path = Path(tmp.name)

    try:
        project = create_project_from_file(session, name=name, upload_path=tmp_path, source_crs=source_crs)
    except UnsupportedFileTypeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        tmp_path.unlink(missing_ok=True)

    return ProjectCreateResponse(project_id=project.id)


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project: ProjectDep) -> ProjectOut:
    return ProjectOut(
        id=project.id,
        name=project.name,
        source_crs=project.source_crs,
        created_at=project.created_at,
        layers=layers_to_feature_collection(project.layers, project.source_crs),
    )
