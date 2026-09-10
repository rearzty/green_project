from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from backend.app.api.deps import get_project_or_404
from backend.app.db.models import Project
from backend.app.db.session import get_session
from backend.app.schemas.project import ProjectCreateResponse, ProjectOut
from backend.app.services.geo_io import layers_to_feature_collection
from backend.app.services.project_service import UnsupportedFileTypeError, create_project_from_file

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.post("", response_model=ProjectCreateResponse)
def upload_project(
    name: str = Form(...),
    source_crs: str | None = Form(None),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
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
def get_project(project: Project = Depends(get_project_or_404)) -> ProjectOut:
    return ProjectOut(
        id=project.id,
        name=project.name,
        source_crs=project.source_crs,
        created_at=project.created_at,
        layers=layers_to_feature_collection(project.layers, project.source_crs),
    )
