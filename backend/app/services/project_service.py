"""Import a territory file into a new Project + its Layer rows.

Layer-name/attribute -> object_type mapping is currently the default
(DEFAULT_LAYER_MAP in geo_engine.io.dxf_reader, raw attribute passthrough for
vector files) since the real Mosgeotrest source layer naming is unknown until
2026-09-15. Swap in a project-specific mapping here once it's known.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models import Layer, Project
from backend.app.services.geo_io import shape_to_db
from geo_engine.io.dxf_reader import read_dxf
from geo_engine.io.shp_geojson_reader import read_vector_file
from geo_engine.model import Utility, Zone

SUPPORTED_SUFFIXES = {".dxf", ".geojson", ".json", ".shp"}


class UnsupportedFileTypeError(ValueError):
    pass


def parse_territory_file(path: Path) -> tuple[list[Utility], list[Zone], str | None]:
    """The third return value is a CRS `read_vector_file` auto-detected from
    the file itself (geographic coordinates only -- see its docstring) --
    None for DXF, which never carries CRS metadata at all, and for a vector
    file that was already metric or had no CRS declared."""
    suffix = path.suffix.lower()
    if suffix == ".dxf":
        utilities, zones = read_dxf(path)
        return utilities, zones, None
    if suffix in (".geojson", ".json", ".shp"):
        return read_vector_file(path, type_field="object_type")
    raise UnsupportedFileTypeError(
        f"Неподдерживаемый формат файла «{suffix or 'без расширения'}». Поддерживаются: {', '.join(sorted(SUPPORTED_SUFFIXES))}."
    )


async def create_project_from_file(
    session: AsyncSession,
    name: str,
    upload_path: Path,
    source_crs: str | None = None,
) -> Project:
    # Parsing (ezdxf/geopandas) is blocking file I/O + CPU work with no
    # async path of its own -- run it off the event loop rather than
    # stalling every other request for however long a large file takes.
    utilities, zones, detected_crs = await run_in_threadpool(parse_territory_file, upload_path)

    # An explicit source_crs from the caller always wins; otherwise fall
    # back to what parse_territory_file auto-detected (geographic files
    # only -- see its docstring). Leaves DXF/already-metric/CRS-less
    # uploads exactly as before.
    project = Project(name=name, source_crs=source_crs or detected_crs)
    session.add(project)
    await session.flush()  # assigns project.id

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

    await session.commit()
    await session.refresh(project)
    return project
