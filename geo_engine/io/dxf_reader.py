"""Import utilities/zones from a DXF drawing.

DXF has no notion of "this layer is a heat network" — that mapping is
project-specific, so the caller supplies a `layer_map: dict[layer_name, (kind,
object_type)]`; unmapped layers fall back to `object_type="unknown"` so nothing
is silently dropped.

Two properties of real drawings that synthetic test fixtures do not have, both
found on the pilot dataset (`Пилотный проект 20 улиц`) and both handled here:

* **Geometry hides inside nested blocks.** The Mosgeotrest geobase is exported
  from MicroStation, and every utility run arrives as an INSERT of a
  single-use block (`msdElementTypeMultiLine`, `msdElementTypeLineString`),
  sometimes nested one level deeper again. Iterating modelspace flat finds
  almost nothing: on `output_1-3__3_ДЖКХ-25_02794up.dwg` the modelspace holds
  50 polylines, while recursive expansion yields 6465 linear entities. Hence
  `explode_blocks`, on by default.
* **Layer names carry an xref prefix.** Bound/attached external references
  namespace their layers as `xref196297|!Граница работ`, so the same logical
  layer appears under as many names as there are sheets referencing it. Lookup
  normalizes on the part after the last `|`.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from collections.abc import Callable
from typing import Iterable, Iterator, Literal

import ezdxf
import ezdxf.recover
from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.base import BaseGeometry

from geo_engine.io.geometry_cleanup import is_origin_artifact, merge_dashed_lines, origin_is_artifact
from geo_engine.io.layer_rules import classify_layer, is_symbol_layer
from geo_engine.model import Utility, Zone

Kind = Literal["utility", "zone"]

# layer name -> (kind, object_type). object_type must match a key in
# planting_norms.yaml's setbacks_m for utilities, or a zone_type geo_engine
# understands ("building", "road", "zoning", "existing_greenery", "territory").
# "territory" is the one zone_type pipeline_service._territory_polygon()
# requires to find the overall site outline — must match across every
# reader (see shp_geojson_reader / generate_synthetic_data.py).
LayerMap = dict[str, tuple[Kind, str]]

DEFAULT_LAYER_MAP: LayerMap = {
    "HEAT_NETWORK": ("utility", "heat_network"),
    "WATER_PIPE": ("utility", "water_pipe"),
    "SEWER": ("utility", "sewer"),
    "GAS_PIPE": ("utility", "gas_pipe"),
    "CABLE_LINE": ("utility", "cable_line"),
    "POWER_LINE": ("utility", "power_line_corridor"),
    "BUILDING": ("zone", "building"),
    "ROAD": ("zone", "road"),
    "ZONING": ("zone", "zoning"),
    "EXISTING_GREENERY": ("zone", "existing_greenery"),
    "BOUNDARY": ("zone", "territory"),
}

# Layer names as they actually appear in the Mosgeotrest geobase sheets and the
# project drawings of the pilot dataset. Cyrillic, mixed case, occasionally
# misspelled upstream ("Опоры контакной сети") — reproduced verbatim, because
# matching is exact.
#
# Three groups, kept distinct on purpose:
#   * direct    — the layer names a utility type outright ("Газопровод").
#   * judgement — a physical obstacle whose geo_engine zone_type is a choice we
#                 made, flagged inline. Revisit if the norms distinguish them.
#   * omitted   — deliberately absent, see NOTES below.
MOSGEOTREST_LAYER_MAP: LayerMap = {
    # --- utilities (geobase "...up" sheets), direct ---
    "Газопровод": ("utility", "gas_pipe"),
    "Теплосеть": ("utility", "heat_network"),
    "Водопровод": ("utility", "water_pipe"),
    "Канализация самотёчная": ("utility", "sewer"),
    "Канализация самотечная": ("utility", "sewer"),  # same layer, "ё" spelled "е"
    "Водосток": ("utility", "sewer"),
    "Водосточный коллектор": ("utility", "sewer"),
    "Кабель электрический": ("utility", "cable_line"),
    "Кабель связи": ("utility", "cable_line"),
    "Кабель защиты": ("utility", "cable_line"),
    "Кабели": ("utility", "cable_line"),
    "ЛЭП": ("utility", "power_line_corridor"),
    # --- zones (geobase "...tp" topographic sheets) ---
    "Здания": ("zone", "building"),
    "Части зданий": ("zone", "building"),
    "Крыльца": ("zone", "building"),  # judgement: a porch is a physical obstacle
    "Навесы": ("zone", "building"),  # judgement: canopy, blocks canopy growth
    "Павильоны": ("zone", "building"),
    "Бортовой камень": ("zone", "road"),  # judgement: the kerb is the carriageway edge
    # --- site outline (project drawings and their xrefs) ---
    # The work area. Lives on this layer in the bound xrefs shipped next to the
    # main drawing (Xrefs/xref196297.dwg, Xrefs/xref192364.dwg on the reference
    # street): two polygons, 29739 + 16132 m², together containing 99% of the
    # designed plantings. The same-named layer in the main drawing holds only
    # stray fragments — territory_polygon() ignores non-areal ones, so mapping
    # both spellings is safe.
    "!Граница работ": ("zone", "territory"),
    "!!!_1. ГРАНИЦА РАБОТ": ("zone", "territory"),
    "_ГП_граница работ": ("zone", "territory"),
    "Леса и газоны": ("zone", "existing_greenery"),
    "Полоса деревьев": ("zone", "existing_greenery"),
    "Отдельно стоящее дерево": ("zone", "existing_greenery"),
}

# NOTES on what is *not* in the map above, so it reads as a decision rather
# than an oversight:
#
# * "Трубопроводы" / "Подземные коммуникации" — generic catch-all layers used
#   for a handful of objects per sheet with no stated medium. planting_norms.yaml
#   has no generic-utility setback and inventing one would silently apply the
#   wrong clearance, so these fall through to "unknown".
# * "Граница улицы" — a survey line along the street edge, not a carriageway
#   polygon; mapping it to "road" would buffer a line that is not the road.
# * "Граница заказа" (geobase "...brd" sheets) — how much survey was ordered,
#   not what is being designed. On the reference street it covers 119011 m²
#   against the work area's 45870, so using it as the territory would invite
#   plantings onto a third of a neighbouring block. Left unmapped on purpose.
# * No "territory" layer. The pilot drawings carry the work-area outline on
#   "!!!_1. ГРАНИЦА РАБОТ" / "!Граница работ", but in the sheets checked those
#   layers hold only stray fragments (2 and 5 vertices) — the real outline lives
#   in an unbound xref. Since pipeline_service._territory_polygon() *requires*
#   zone_type="territory", a DXF import of this dataset still needs the site
#   outline supplied another way. Open question, not a mapping we can guess.

# Layers whose INSERTs are point symbols: the insertion point *is* the object,
# and exploding them yields the little circles and ticks the symbol is drawn
# from instead of one feature per object.
SYMBOL_LAYERS: frozenset[str] = frozenset(
    {
        "Отдельно стоящее дерево",
        "Колодцы",
        "Люки",
        "Фонари",
        "Столбы",
        "Геодезические пункты",
    }
)

# Both vocabularies at once. The keys cannot collide — one set is ASCII
# uppercase, the other Cyrillic — so a caller that does not know which kind of
# drawing it was handed can just use this.
COMBINED_LAYER_MAP: LayerMap = {**DEFAULT_LAYER_MAP, **MOSGEOTREST_LAYER_MAP}


def stitch_utility_lines(utilities: list[Utility]) -> list[Utility]:
    """Rebuild dashed utility runs, one object_type at a time.

    Per type, not globally: a gas dash and a water dash can lie collinear and
    within a dash gap of each other where two mains share a trench, and joining
    those would produce a line that is neither.

    Non-line geometry (a manhole circle, a symbol point) passes through
    untouched — there is nothing to stitch, and it still constrains plantings.
    """
    by_type: dict[str, list[Utility]] = {}
    passthrough: list[Utility] = []
    for utility in utilities:
        if utility.geometry.geom_type in ("LineString", "MultiLineString"):
            by_type.setdefault(utility.object_type, []).append(utility)
        else:
            passthrough.append(utility)

    stitched: list[Utility] = []
    for object_type, group in by_type.items():
        layer_source = group[0].layer_source
        for line in merge_dashed_lines([u.geometry for u in group]):
            stitched.append(Utility(geometry=line, object_type=object_type, layer_source=layer_source))
    return stitched + passthrough


# Guard against a self-referencing or pathologically deep block structure. The
# pilot drawings nest two levels; anything past this is not real content.
_MAX_BLOCK_DEPTH = 8

# A polyline drawn as a closed outline but never flagged closed still has to
# read as an area. The work-boundary outline in the pilot xrefs is exactly
# that: 331 vertices, is_closed=False, first and last points 11 mm apart over a
# 1833 m perimeter. 5 cm is two orders above that and far below any gap someone
# left on purpose. Guarded by a vertex count so a stray 3-point line whose ends
# happen to land near each other stays a line.
_POLYLINE_CLOSE_TOLERANCE_M = 0.05
_POLYLINE_CLOSE_MIN_POINTS = 4


def normalize_layer(name: str) -> str:
    """Strip the `xrefname|` prefix an external reference adds to its layers."""
    return name.rsplit("|", 1)[-1]


def _entity_to_geometry(entity) -> BaseGeometry | None:
    dxftype = entity.dxftype()

    if dxftype == "LINE":
        start, end = entity.dxf.start, entity.dxf.end
        return LineString([(start.x, start.y), (end.x, end.y)])

    if dxftype in ("LWPOLYLINE", "POLYLINE"):
        points = [(p[0], p[1]) for p in entity.get_points()] if dxftype == "LWPOLYLINE" else [
            (v.dxf.location.x, v.dxf.location.y) for v in entity.vertices
        ]
        if len(points) < 2:
            return None
        is_closed = getattr(entity, "is_closed", False) or getattr(entity.dxf, "flags", 0) & 1
        if is_closed and len(points) >= 3:
            return Polygon(points)
        if len(points) >= _POLYLINE_CLOSE_MIN_POINTS:
            gap = math.dist(points[0], points[-1])
            if gap <= _POLYLINE_CLOSE_TOLERANCE_M:
                candidate = Polygon(points)
                if candidate.is_valid and candidate.area > 0:
                    return candidate
        return LineString(points)

    if dxftype == "CIRCLE":
        center = entity.dxf.center
        return Point(center.x, center.y).buffer(entity.dxf.radius)

    if dxftype == "POINT":
        loc = entity.dxf.location
        return Point(loc.x, loc.y)

    if dxftype == "INSERT":
        insert = entity.dxf.insert
        return Point(insert.x, insert.y)

    return None


def iter_entities(container, explode_blocks: bool = True, symbol_layers: frozenset[str] | None = None) -> Iterator:
    """Yield drawable entities, descending into block references.

    An INSERT on a symbol layer is yielded as-is (its insertion point is the
    feature); any other INSERT is replaced by its contents, recursively.
    """
    symbol_layers = SYMBOL_LAYERS if symbol_layers is None else symbol_layers

    def walk(entities, depth: int) -> Iterator:
        for entity in entities:
            if entity.dxftype() != "INSERT":
                yield entity
                continue
            layer = normalize_layer(entity.dxf.layer)
            if layer in symbol_layers or is_symbol_layer(layer):
                yield entity
                continue
            if not explode_blocks or depth >= _MAX_BLOCK_DEPTH:
                continue
            try:
                virtual = list(entity.virtual_entities())
            except Exception:
                # A block ezdxf cannot expand (unresolved xref, malformed
                # definition) must not abort the whole import.
                continue
            yield from walk(virtual, depth + 1)

    yield from walk(container, 0)


def read_document(path: Path):
    """Открыть DXF, по возможности пережив повреждения.

    `ezdxf.readfile()` разбирает файл строго и отказывается целиком, если хоть
    где-то нарушена структура групповых кодов. На чертежах, прошедших через
    конвертацию из DWG, это случается: у одной улицы пилота файл ломается на
    6.5-миллионной строке из-за одной испорченной надписи, и вместе с ним
    терялась бы вся улица. `ezdxf.recover` собирает то, что удалось разобрать.

    Сначала быстрый путь, восстановление — только как запасной: recover заметно
    медленнее, и гонять его на исправных файлах незачем.
    """
    try:
        return ezdxf.readfile(str(path))
    except (ezdxf.DXFStructureError, UnicodeDecodeError):
        doc, _auditor = ezdxf.recover.readfile(str(path))
        return doc


def _warn_unreadable(path: Path, error: Exception) -> None:
    """Файл бандла, который не удалось прочитать даже восстановлением.

    Печатается, а не проглатывается: молчаливый пропуск — это как раз тот
    случай, когда потерянная граница участка выглядит как отсутствующая, и
    искать причину пришлось бы в алгоритме, а не в данных.
    """
    print(f"  ! не прочитан {path.name}: {type(error).__name__}: {str(error)[:120]}", file=sys.stderr)


# Как в пилотном датасете называют папку внешних ссылок. Одного имени мало:
# по девятнадцати улицам встретились `Xrefs`, `xref`, `xref_ИТП`, `ссылки`,
# `00_Ссылки`, `Внешние ссылки`, `Вн.ссылки` — у каждого бюро своё. Сравнение
# идёт по подстроке в нижнем регистре, поэтому числовые префиксы (`00_`) и
# суффиксы (`_ИТП`) не мешают.
XREF_DIR_MARKERS = ("xref", "ссылк")


def _looks_like_xref_dir(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in XREF_DIR_MARKERS)


def dxf_bundle_paths(main_path: str | Path, xref_dirname: str | None = None) -> list[Path]:
    """The main drawing plus the external references sitting next to it.

    A project drawing is not self-contained: the pilot street keeps its geobase
    sheets and — critically — its work-area outline in an xref folder beside the
    main file, and `ezdxf` does not resolve those (they are DWG, and an attached
    xref is a file reference, not embedded content). Reading the main file alone
    gets the design but no site boundary and no utilities.

    `xref_dirname` pins one folder name; without it every sibling directory
    whose name looks like an xref folder is taken (see XREF_DIR_MARKERS —
    hardcoding "Xrefs" missed most streets, which call it `00_Ссылки`,
    `Внешние ссылки`, `xref_ИТП` and so on).

    Returned in a stable order, main file first, and only files that exist.
    """
    main_path = Path(main_path)
    paths = [main_path]

    parent = main_path.parent
    if xref_dirname is not None:
        candidates = [parent / xref_dirname]
    else:
        candidates = sorted(p for p in parent.iterdir() if p.is_dir() and _looks_like_xref_dir(p.name))

    for xref_dir in candidates:
        if xref_dir.is_dir():
            paths.extend(sorted(p for p in xref_dir.iterdir() if p.suffix.lower() == ".dxf"))
    return [p for p in paths if p.is_file()]


def read_dxf_bundle(
    paths: Iterable[str | Path],
    layer_map: LayerMap | None = None,
    explode_blocks: bool = True,
    symbol_layers: frozenset[str] | None = None,
    stitch_dashes: bool = False,
    drop_origin: bool = False,
    use_layer_rules: bool = True,
    on_error: Callable[[Path, Exception], None] | None = _warn_unreadable,
) -> tuple[list[Utility], list[Zone]]:
    """Read several DXF files as one drawing.

    Plain concatenation is correct here, and that is worth stating because it
    would not be in general: an attached xref can carry its own insertion point,
    scale and rotation. In the pilot data it does not — every sheet of a street
    is in the same coordinate system, verified by the existing trees landing on
    the same coordinates in the topographic sheet and in the dendroplan. A
    bundle whose xrefs are transformed would need those transforms applied
    first; this does not attempt to.

    Stitching runs once over the merged result rather than per file, so a run
    split across two sheets still joins up.

    `on_error` решает судьбу файла, который не читается даже восстановлением.
    По умолчанию — предупредить и продолжить: один повреждённый вспомогательный
    чертёж не должен уносить с собой всю улицу (живой случай на Измайловской
    площади). Передайте функцию, которая пробрасывает исключение, если для
    вашего сценария потеря любого файла недопустима.
    """
    utilities: list[Utility] = []
    zones: list[Zone] = []
    for path in paths:
        try:
            file_utilities, file_zones = read_dxf(
                path,
                layer_map=layer_map,
                explode_blocks=explode_blocks,
                symbol_layers=symbol_layers,
                stitch_dashes=False,
                drop_origin=drop_origin,
                use_layer_rules=use_layer_rules,
            )
        except Exception as error:  # noqa: BLE001 — судьбу решает вызывающий
            if on_error is None:
                raise
            on_error(Path(path), error)
            continue
        utilities.extend(file_utilities)
        zones.extend(file_zones)

    if stitch_dashes:
        utilities = stitch_utility_lines(utilities)
    return utilities, zones


def resolve_layer(layer: str, layer_map: LayerMap, use_rules: bool = True) -> tuple[Kind, str]:
    """Тип объекта для слоя: сначала дословная карта, потом образцы имени.

    Дословная карта главнее и проверяется первой. Она однозначна и сверена, а
    правило по подстроке неизбежно приблизительно — там, где имя известно
    точно, гадать незачем.

    Правила нужны потому, что дословная карта покрывала семь улиц пилота из
    девятнадцати: геоподоснова называет слои единообразно, а проектные чертежи
    у каждого бюро свои. Слой, не опознанный ни картой, ни правилами, уходит в
    "unknown" — как и раньше, чтобы ничего не терялось молча.
    """
    mapped = layer_map.get(layer)
    if mapped is not None:
        return mapped
    if use_rules:
        guessed = classify_layer(layer)
        if guessed is not None:
            return guessed
    return ("zone", "unknown")


def read_dxf(
    path: str | Path,
    layer_map: LayerMap | None = None,
    explode_blocks: bool = True,
    symbol_layers: frozenset[str] | None = None,
    stitch_dashes: bool = False,
    drop_origin: bool = False,
    use_layer_rules: bool = True,
) -> tuple[list[Utility], list[Zone]]:
    """Parse a DXF file's modelspace into Utility and Zone lists, keyed by
    layer name via `layer_map` (defaults to DEFAULT_LAYER_MAP).

    `stitch_dashes` and `drop_origin` are the CAD-export cleanups described in
    geometry_cleanup — off by default because they only apply to drawings that
    have those artifacts, and a caller reading a clean DXF should get exactly
    what the file contains.

    `use_layer_rules` включает распознавание слоя по образцу имени, когда
    дословной записи в карте нет (см. layer_rules). По умолчанию включено:
    без него читались семь улиц пилота из девятнадцати.
    """
    layer_map = layer_map or DEFAULT_LAYER_MAP
    doc = read_document(Path(path))
    msp = doc.modelspace()

    utilities: list[Utility] = []
    zones: list[Zone] = []

    for entity in iter_entities(msp, explode_blocks=explode_blocks, symbol_layers=symbol_layers):
        geometry = _entity_to_geometry(entity)
        if geometry is None or geometry.is_empty:
            continue

        layer = normalize_layer(entity.dxf.layer)
        kind, object_type = resolve_layer(layer, layer_map, use_layer_rules)

        if kind == "utility":
            utilities.append(Utility(geometry=geometry, object_type=object_type, layer_source=layer))
        else:
            zones.append(Zone(geometry=geometry, zone_type=object_type, attrs={"layer": layer}))

    if drop_origin:
        # One decision over the whole extraction, then a per-object predicate:
        # whether geometry on the origin is junk depends on where the rest of
        # the drawing sits, but the drop itself is per object.
        everything = [u.geometry for u in utilities] + [z.geometry for z in zones]
        if origin_is_artifact(everything):
            utilities = [u for u in utilities if not is_origin_artifact(u.geometry)]
            zones = [z for z in zones if not is_origin_artifact(z.geometry)]
    if stitch_dashes:
        utilities = stitch_utility_lines(utilities)

    return utilities, zones
