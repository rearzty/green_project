"""Import a territory file into a new Project + its Layer rows.

DXF goes in through COMBINED_LAYER_MAP, which holds both the project's own
uppercase layer names and the Cyrillic ones the Mosgeotrest geobase and the
pilot project drawings actually use — the keys cannot collide, so one map
serves an export of ours and a real survey sheet alike. Vector files still pass
attributes through raw.

DXF is also read with the CAD-export cleanups on (`stitch_dashes`,
`drop_origin`, `reconstruct_footprints`): an upload here is a real drawing,
and on real drawings utility runs arrive exploded into linetype dashes, the
legend sits at the origin, and a building outline arrives as an unclosed
polyline rather than a polygon (real building geometry, not just visual
completeness -- see geometry_cleanup.reconstruct_closed_footprints's
docstring for the buildable_area consequence of leaving it as a line). See
geo_engine.io.geometry_cleanup for what each one does and does not touch.

A real project drawing is not self-contained -- see
geo_engine.io.dxf_reader.resolve_bundle_inputs's docstring. Two more suffixes
close that gap here:

* `.dwg` -- a single drawing, converted to DXF on the way in
  (geo_engine.io.dwg_convert, LibreDWG or ODA File Converter, whichever is on
  PATH -- see infra/Dockerfile.backend for how the image gets one).
* `.zip` -- a whole project folder (the main drawing plus its `Xrefs/`),
  zipped so an HTML file input can carry it as one upload. Extracted to a
  scratch directory and read the same way `scripts/flatten_bundle.py` /
  `scripts/plan_dxf.py` do: `resolve_bundle_inputs` picks the largest
  .dxf/.dwg in the archive root as the main drawing, converts every DWG in the
  bundle, and `read_dxf_bundle` merges them into one set of utilities/zones.
  On the pilot data this is what actually gets a site boundary into the
  project -- it lives in the xrefs, not the main file.
"""

from __future__ import annotations

import shutil
import tempfile
import zipfile
from pathlib import Path

from fastapi.concurrency import run_in_threadpool

from backend.app.db import session as store
from backend.app.db.models import Layer, Project
from backend.app.services.geo_io import shape_to_db
from geo_engine.io.dxf_reader import (
    COMBINED_LAYER_MAP,
    BundleResolutionError,
    read_dxf,
    resolve_and_read_bundle,
)
from geo_engine.io.shp_geojson_reader import read_vector_file
from geo_engine.model import Utility, Zone

SUPPORTED_SUFFIXES = {".dxf", ".dwg", ".zip", ".geojson", ".json", ".shp"}


class UnsupportedFileTypeError(ValueError):
    pass


def _parse_dxf_bundle(path: Path, workdir: Path) -> tuple[list[Utility], list[Zone], str | None]:
    """One drawing or a whole project folder -- resolve_and_read_bundle
    handles both, so a lone .dwg and a .zip full of them share this path.

    `resolve_and_read_bundle` (not the older resolve_bundle_inputs +
    read_dxf_bundle two-step) overlaps DWG->DXF conversion with reading
    instead of waiting for the whole bundle to finish converting before
    reading anything -- see its docstring. This is the only caller that
    matters for that overlap: the web upload is exactly the path a slow,
    fully-sequential resolve-then-read was measured live to cost tens of
    seconds on a real multi-file bundle.
    """
    utilities, zones, warnings = resolve_and_read_bundle(
        path, workdir, layer_map=COMBINED_LAYER_MAP, stitch_dashes=True, drop_origin=True, reconstruct_footprints=True
    )
    for warning in warnings:
        # Not fatal -- one unreadable xref shouldn't sink the whole upload --
        # but worth keeping somewhere a developer can find it; nothing in the
        # API response surfaces per-file warnings today.
        print(f"  ! {warning}")
    return utilities, zones, None


# Флаг «имена внутри архива в UTF-8» (general purpose bit 11, APPNOTE 4.4.4).
# Без него zipfile обязан считать имена CP437 — см. _zip_member_name.
_ZIP_UTF8_FLAG = 0x800


def _zip_member_name(info: zipfile.ZipInfo) -> str:
    """Имя файла из архива в правильной кодировке.

    Живой отказ, ради которого это написано: `archive.extractall()` на
    обычном архиве папки улицы падал `OSError: [Errno 36] File name too
    long`, и загрузка ZIP была нерабочей ровно на том входе, ради которого
    делалась — на русских папках с чертежами.

    Механика. Спецификация ZIP знает ровно две кодировки имён: CP437 и (если
    выставлен бит 11) UTF-8. Ни Finder, ни Info-ZIP `zip`, ни проводник
    Windows этот бит не ставят, а имена пишут в UTF-8 — то есть архив лжёт о
    себе, и `zipfile` честно декодирует UTF-8-байты как CP437. Каждый
    кириллический символ превращается в два псевдографических, и имя пухнет:
    замерено на реальном чертеже — 229 байт стали 566 при лимите имени
    файла в 255. Отсюда и `File name too long`, хотя настоящее имя короче
    лимита втрое.

    Восстановление точное, а не эвристика: `.encode("cp437")` возвращает
    ровно те байты, что лежат в архиве, и их уже можно прочитать как UTF-8.
    Если архив действительно старый и действительно в CP437, обратный
    разбор не сложится (UnicodeDecodeError) — тогда правильным и остаётся
    то, что вернул zipfile.
    """
    if info.flag_bits & _ZIP_UTF8_FLAG:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def _extract_archive(archive: zipfile.ZipFile, extract_dir: Path) -> None:
    """Распаковать архив, читая имена через `_zip_member_name`.

    Поэлементно, а не `extractall()`, только по одной причине: имя надо
    исправить до того, как по нему создаётся файл, а `extractall` такого
    хука не даёт.

    Раз распаковка теперь ручная, проверку выхода за пределы каталога
    (zip-slip: запись с именем вроде `../../etc/passwd`) приходится делать
    самим — `extractall` её содержит, и потерять её молча при переходе на
    ручной цикл было бы обидно.
    """
    root = extract_dir.resolve()
    for info in archive.infolist():
        target = (extract_dir / _zip_member_name(info)).resolve()
        if not target.is_relative_to(root):
            continue
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as source, open(target, "wb") as sink:
            shutil.copyfileobj(source, sink)


def _parse_zip_bundle(path: Path, workdir: Path) -> tuple[list[Utility], list[Zone], str | None]:
    extract_dir = workdir / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(path) as archive:
            _extract_archive(archive, extract_dir)
    except zipfile.BadZipFile as error:
        raise UnsupportedFileTypeError("Файл .zip повреждён или не является архивом.") from error
    except OSError as error:
        # Имя, которое файловая система не принимает даже после исправления
        # кодировки. Пользователю нужен понятный отказ, а не 500 с трейсбеком:
        # именно так этот баг и выглядел снаружи — «нет связи с сервером».
        raise UnsupportedFileTypeError(
            f"Не удалось распаковать архив: {error.strerror or error}."
        ) from error

    # A folder zipped on macOS/Windows often lands one level down (the zip
    # root holds a single directory named after the folder) or wrapped in a
    # __MACOSX/ sibling -- look past both rather than require the archive be
    # built just so.
    entries = [p for p in extract_dir.iterdir() if p.name != "__MACOSX"]
    root = entries[0] if len(entries) == 1 and entries[0].is_dir() else extract_dir
    return _parse_dxf_bundle(root, workdir)


def parse_territory_file(path: Path, workdir: Path | None = None) -> tuple[list[Utility], list[Zone], str | None]:
    """The third return value is a CRS `read_vector_file` auto-detected from
    the file itself (geographic coordinates only -- see its docstring) --
    None for DXF/DWG/ZIP, which never carry CRS metadata at all, and for a
    vector file that was already metric or had no CRS declared.

    `workdir` is where a DWG gets converted / a ZIP gets extracted; the caller
    owns its lifetime (create_project_from_file wraps the whole call in a
    TemporaryDirectory) because the geometry this function returns doesn't
    outlive it -- nothing here keeps its own copy of the drawing.
    """
    suffix = path.suffix.lower()
    if suffix == ".dxf":
        utilities, zones = read_dxf(
            path, layer_map=COMBINED_LAYER_MAP, stitch_dashes=True, drop_origin=True, reconstruct_footprints=True
        )
        return utilities, zones, None
    if suffix == ".dwg":
        if workdir is None:
            raise ValueError("parse_territory_file(.dwg) requires workdir")
        try:
            return _parse_dxf_bundle(path, workdir)
        except (BundleResolutionError, RuntimeError) as error:
            # RuntimeError too, not just BundleResolutionError: that's only
            # "no converter on PATH" -- a converter that IS present but chokes
            # on this specific file (corrupt DWG, unsupported version) raises
            # plain RuntimeError from dwg_convert.convert_with_libredwg, and
            # that deserves the same clean 400, not a raw traceback.
            raise UnsupportedFileTypeError(str(error)) from error
    if suffix == ".zip":
        if workdir is None:
            raise ValueError("parse_territory_file(.zip) requires workdir")
        try:
            return _parse_zip_bundle(path, workdir)
        except (BundleResolutionError, RuntimeError) as error:
            raise UnsupportedFileTypeError(str(error)) from error
    if suffix in (".geojson", ".json", ".shp"):
        return read_vector_file(path, type_field="object_type")
    raise UnsupportedFileTypeError(
        f"Неподдерживаемый формат файла «{suffix or 'без расширения'}». Поддерживаются: {', '.join(sorted(SUPPORTED_SUFFIXES))}."
    )


async def create_project_from_file(
    name: str,
    upload_path: Path,
    source_crs: str | None = None,
) -> Project:
    # Parsing (ezdxf/geopandas) is blocking file I/O + CPU work with no
    # async path of its own -- run it off the event loop rather than
    # stalling every other request for however long a large file takes. The
    # workdir (DWG conversion output, ZIP extraction) lives only as long as
    # this call -- everything it produces is read into `utilities`/`zones`
    # (plain shapely geometry) before it's cleaned up.
    with tempfile.TemporaryDirectory(prefix="greenproject-upload-") as workdir:
        utilities, zones, detected_crs = await run_in_threadpool(
            parse_territory_file, upload_path, Path(workdir)
        )

    # An explicit source_crs from the caller always wins; otherwise fall
    # back to what parse_territory_file auto-detected (geographic files
    # only -- see its docstring). Leaves DXF/already-metric/CRS-less
    # uploads exactly as before.
    #
    # crs_verified is deliberately narrower than "we have a source_crs at
    # all": it's only true when *auto-detection* is what produced it, from
    # real evidence in the file's own coordinates -- a caller-supplied
    # source_crs (even one that happens to be correct) is still a guess we
    # have no way to check, and showing a real basemap under it would be
    # exactly the kind of overconfidence that produced the Kenya-map bug
    # (see CLAUDE.md). See Project.crs_verified's own docstring.
    project = Project(name=name, source_crs=source_crs or detected_crs, crs_verified=source_crs is None and detected_crs is not None)
    project.layers = [
        Layer(
            project_id=project.id,
            kind="utility",
            object_type=utility.object_type,
            geometry=shape_to_db(utility.geometry),
            attrs=utility.attrs,
        )
        for utility in utilities
    ] + [
        Layer(
            project_id=project.id,
            kind="zone",
            object_type=zone.zone_type,
            geometry=shape_to_db(zone.geometry),
            attrs=zone.attrs,
        )
        for zone in zones
    ]

    store.save_project(project)
    return project
