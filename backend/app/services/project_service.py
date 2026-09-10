"""Import a territory file into a new Project + its Layer rows.

Layer-name/attribute -> object_type mapping is currently the default
(DEFAULT_LAYER_MAP in geo_engine.io.dxf_reader, raw attribute passthrough for
vector files) since the real Mosgeotrest source layer naming is unknown until
2026-09-15. Swap in a project-specific mapping here once it's known.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from backend.app.db.models import Layer, Project
from backend.app.services.geo_io import shape_to_db
from geo_engine.io.dxf_reader import read_dxf
from geo_engine.io.shp_geojson_reader import read_vector_file
from geo_engine.model import Utility, Zone

SUPPORTED_SUFFIXES = {".dxf", ".geojson", ".json", ".shp"}


class UnsupportedFileTypeError(ValueError):
    pass


def parse_territory_file(path: Path) -> tuple[list[Utility], list[Zone]]:
    suffix = path.suffix.lower()
    if suffix == ".dxf":
        return read_dxf(path)
    if suffix in (".geojson", ".json", ".shp"):
        return read_vector_file(path, type_field="object_type")
    raise UnsupportedFileTypeError(
        f"Unsupported file type '{suffix}'. Supported: {', '.join(sorted(SUPPORTED_SUFFIXES))}"
    )


def create_project_from_file(
    session: Session,
    name: str,
    upload_path: Path,
    source_crs: str | None = None,
) -> Project:
    utilities, zones = parse_territory_file(upload_path)

    project = Project(name=name, source_crs=source_crs)
    session.add(project)
    session.flush()  # assigns project.id

    for utility in utilities:
        session.add(
            Layer(
                project_id=project.id,
                kind="utility",
                object_type=utility.object_type,
                geometry=shape_to_db(utility.geometry),
                attrs=utility.attrs,
            )
        )
    for zone in zones:
        session.add(
            Layer(
                project_id=project.id,
                kind="zone",
                object_type=zone.zone_type,
                geometry=shape_to_db(zone.geometry),
                attrs=zone.attrs,
            )
        )

    session.commit()
    session.refresh(project)
    return project
