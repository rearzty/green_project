"""Import a territory file into a new Project + its Layer rows.

DXF goes in through COMBINED_LAYER_MAP, which holds both the project's own
uppercase layer names and the Cyrillic ones the Mosgeotrest geobase and the
pilot project drawings actually use — the keys cannot collide, so one map
serves an export of ours and a real survey sheet alike. Vector files still pass
attributes through raw.

DXF is also read with the CAD-export cleanups on (`stitch_dashes`,
`drop_origin`): an upload here is a real drawing, and on real drawings utility
runs arrive exploded into linetype dashes and the legend sits at the origin.
See geo_engine.io.geometry_cleanup for what each one does and does not touch.

Known limit of single-file upload: a real project drawing is not
self-contained. On the pilot data the work-area outline — the zone_type
="territory" that pipeline_service.territory_polygon() requires — is not in the
main drawing at all; it sits on layer "!Граница работ" in the xrefs shipped
beside it (Xrefs/xref196297.dwg, Xrefs/xref192364.dwg), and so do the
utilities. geo_engine.io.dxf_reader.read_dxf_bundle() reads a drawing together
with its Xrefs/ directory and produces both. Wiring that to the API needs an
upload that can carry more than one file (a ZIP of the drawing folder); until
then, a single-DXF upload of such a project imports whatever that one file
holds and may well have no site boundary.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models import Layer, Project
from backend.app.services.geo_io import shape_to_db
from geo_engine.io.dxf_reader import COMBINED_LAYER_MAP, read_dxf
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
        utilities, zones = read_dxf(
            path, layer_map=COMBINED_LAYER_MAP, stitch_dashes=True, drop_origin=True
        )
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
