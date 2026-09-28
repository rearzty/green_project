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
import os
import sys
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path
from collections.abc import Callable
from typing import Iterable, Iterator, Literal

import ezdxf
import ezdxf.recover
import ezdxf.lldxf.encoding
import ezdxf.entities.mtext
import ezdxf.layouts.layouts
import ezdxf.lldxf.const
import ezdxf.acis.api
import ezdxf.sections.acdsdata
from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

# Live bug, found on 3 of the pilot streets (Старый Гай, Берзарина,
# Академика Понтрягина): a DWG's embedded material/plot-style metadata can
# carry a raw byte that isn't valid in the file's stated encoding; ezdxf
# decodes it with errors="surrogateescape", producing a lone UTF-16
# surrogate character glued right up against whatever text follows. That
# collides with ezdxf's own `\U+xxxx`-escape decoder
# (ezdxf/lldxf/encoding.py): `decode_dxf_unicode()` splits on the strict
# 4-hex-digit pattern and hands every *unmatched* leftover to `_decode()`
# too, which still tries `chr(int(s[3:], 16))` on anything starting with the
# literal `\U+` -- a truncated escape landing next to the corrupted
# surrogate satisfies that `startswith` check with zero or garbage hex
# digits after it, and `_decode` raises `ValueError` with no fallback of its
# own. This is the *recovery* path (`ezdxf.recover`, meant to survive
# exactly this kind of damage) failing on the same bug -- `read_document()`
# below has nothing further to fall back to once this raises. Patched here,
# not fixed upstream: leave an escape we can't decode as literal text
# instead of aborting the whole file: that's the same "graceful degradation
# over crash" this module already applies to a torn block or an unreadable
# xref, just one call deeper.
_original_decode_dxf_char = ezdxf.lldxf.encoding._decode


def _decode_dxf_char_or_keep_literal(s: str) -> str:
    try:
        return _original_decode_dxf_char(s)
    except ValueError:
        return s


ezdxf.lldxf.encoding._decode = _decode_dxf_char_or_keep_literal

# Live bug, found on 13. Харьковский проезд (the separately-supplied
# electrical drawing, `ЭС_Харьковский проезд.dwg`): the same class of
# embedded-metadata corruption that motivates the `\U+` patch above can also
# desync a downstream MTEXT entity's XDATA-encoded column-layout group codes
# (`ACAD_MTEXT_COLUMN_INFO`, group code 75 inside a 1070-tagged xdata stream)
# far enough that `ColumnType(value)` gets handed a value no member matches
# -- 1434 seen live. `enum.IntEnum` has no fallback of its own here, and this
# is Python enum construction during entity loading, not DXF group-code
# tokenizing -- `ezdxf.recover`'s own per-tag resilience (the thing
# `read_document()` below falls back to) does not reach this far, so the
# `ValueError` aborts the whole file in both readfile and recover modes.
# MTEXT's multi-column layout is a cosmetic text-formatting detail this
# project's pipeline never reads (only geometry is extracted for
# utilities/zones), so an unrecognised column type is safe to treat as "no
# special column layout" (`NONE`) instead of letting it abort the file.
ezdxf.entities.mtext.ColumnType._missing_ = classmethod(lambda cls, value: cls.NONE)

# Живой отказ на «12. Наташинский пр-д» (`04.Графическая часть.dxf`):
# загрузка документа падала `KeyError: 'ГЕНПЛАН2.1'`, файл целиком
# пропускался, а вместе с ним терялась граница участка — то есть улица
# переставала считаться из-за одной записи в таблице лейаутов.
#
# Это дефект контракта внутри самого ezdxf, а не порча данных.
# `Layouts.get_layout_by_key()` на каждом пути отказа аккуратно поднимает
# `DXFKeyError` — и вызывающий `DXFGraphic.get_layout()` ловит ровно его,
# после чего корректно деградирует до `None`. Но ПОСЛЕДНЯЯ строка
# (`return self.get(dxf_layout.dxf.name)`) обращается к словарю
# `self._layouts` напрямую и бросает ГОЛЫЙ `KeyError`, мимо собственного
# контракта функции: имя лейаута есть в записи блока, но самого лейаута в
# словаре нет. Этот `KeyError` проходит сквозь обработчик вызывающего и
# убивает загрузку целиком — причём в пост-обработке MTEXT-колонок, то есть
# на пути, который `ezdxf.recover` уже не страхует.
#
# Патч восстанавливает контракт, а не меняет поведение: тот же отказ теперь
# выглядит как `DXFKeyError`, вызывающий его ловит, `get_layout()` возвращает
# `None`, и закрытие колонок уходит в свою же ветку `else`
# (`mtext.dxf.owner = None`). Многоколоночная разметка MTEXT — косметика,
# которую этот пайплайн вообще не читает (извлекается только геометрия), ровно
# как и в патче `ColumnType` выше.
_original_get_layout_by_key = ezdxf.layouts.layouts.Layouts.get_layout_by_key


def _get_layout_by_key_honouring_contract(self, layout_key):
    try:
        return _original_get_layout_by_key(self, layout_key)
    except ezdxf.lldxf.const.DXFKeyError:
        raise
    except KeyError as error:
        raise ezdxf.lldxf.const.DXFKeyError(
            f'Layout with key "{layout_key}" does not exist.'
        ) from error


ezdxf.layouts.layouts.Layouts.get_layout_by_key = _get_layout_by_key_honouring_contract

# Live performance bug, found on "4. Харьковская улица" (a real coverage-fill
# xref, 6503 REGION entities in one 60.7 МБ file): every entity.sab access --
# what _region_to_geometry() below triggers once per REGION/3DSOLID via
# ezdxf.acis.api.load_dxf() -- makes ezdxf.sections.acdsdata.AcDsDataSection
# .find_acis_record() do a LINEAR SCAN through the whole ACDSDATA section
# looking for a matching handle. One file with N ACIS entities therefore
# costs O(N²), not O(N), and it is not a small effect: measured directly on
# this file, per-entity cost grows from 8.3ms (entities 0-200) to 18.0ms
# (entities 6000-6200) as the scan gets longer, and REGION-to-geometry
# conversion alone took 104s of this file's 106s read -- almost the entire
# per-file bottleneck of the whole 19-file bundle read (103s of a 224.5s
# CLI run). Patched here, not fixed upstream: build the handle -> record
# index once per section (O(N)) and reuse it, invalidating only when
# `entities`'s length actually changes (a write path -- set_acis_data/
# new_acis_data/del_acis_data -- appending or removing a record) -- turns
# every lookup into O(1) and the whole document's ACIS reads into O(N).
def _indexed_find_acis_record(self, handle: str):
    entities = self.entities
    index = getattr(self, "_greenproject_acis_index", None)
    if index is None or getattr(self, "_greenproject_acis_index_len", -1) != len(entities):
        index = {}
        for record in self.acdsrecords:
            if ezdxf.sections.acdsdata.is_acis_data(record):
                index[ezdxf.sections.acdsdata.acis_entity_handle(record)] = record
        self._greenproject_acis_index = index
        self._greenproject_acis_index_len = len(entities)
    return index.get(handle)


ezdxf.sections.acdsdata.AcDsDataSection.find_acis_record = _indexed_find_acis_record

from geo_engine.io.geometry_cleanup import (
    DEFAULT_DANGLE_BUFFER_M,
    DEFAULT_SNAP_GRID_M,
    is_origin_artifact,
    merge_dashed_lines,
    origin_is_artifact,
    reconstruct_closed_footprints,
    reconstruct_closed_road_polygons,
)
from geo_engine.io.layer_rules import classify_layer, is_symbol_layer
from geo_engine.model import Utility, Zone

Kind = Literal["utility", "zone"]

# layer name -> (kind, object_type). object_type must match a key in
# planting_norms.yaml's setbacks_m for utilities, or a zone_type geo_engine
# understands ("building", "road", "zoning", "existing_greenery",
# "existing_lawn", "territory"). "existing_lawn" is NOT a hard obstacle
# (unlike "existing_greenery") -- see layer_rules.py's "гзн" rule docstring.
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
    # street "2. Песчаный переулок"): two polygons, 29739 + 16132 m², together
    # containing 99% of the designed plantings. The same-named layer in the
    # main drawing holds only stray fragments — territory_polygon() ignores
    # non-areal ones, so mapping every spelling below is safe even where one
    # street's file only has fragments on it and the real outline is
    # elsewhere. A fourth spelling ("ДВ_ГП_П_Граница работ") confirmed live on
    # "1. Олимпийская деревня"'s ссылки/10000176_Границы работ_Олимп.dwg — a
    # different source/drafter for that street, same role. A fifth
    # ("Граница проектирования") confirmed live on "7. Нижние Поля ул" —
    # same fragment-only pattern as the others there (one 2-vertex open
    # LWPOLYLINE on the main drawing), real outline still elsewhere in that
    # street's bundle. Expect more spellings across the other streets; this
    # list is not claimed complete.
    "!Граница работ": ("zone", "territory"),
    "!!!_1. ГРАНИЦА РАБОТ": ("zone", "territory"),
    "_ГП_граница работ": ("zone", "territory"),
    "ДВ_ГП_П_Граница работ": ("zone", "territory"),
    "Граница проектирования": ("zone", "territory"),
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
# * "Люки" / "Колодцы" (already in SYMBOL_LAYERS below) — checked against
#   every available norm (SP 42.13330.2016 table 9.1, 743-PP tables 3.6.1/
#   3.6.2, MGSN 1.02-02, SP 82.13330.2016, GOST 21.508-2020) and none gives a
#   planting setback FROM a manhole/hatch. The one real hit (MGSN 1.02-02
#   §6.4.3) is a rule about where the manhole itself may sit, scoped to
#   school/kindergarten plots, not a tree/shrub distance. Confirmed live on
#   "4. Харьковская улица" that this isn't a data gap either: the raw survey
#   layer is bare ARC+TEXT (no INSERT block, no network-type marker — the
#   TEXT is just a spot elevation), while the actual pipe it sits on is
#   already present and already classified elsewhere (water_pipe/sewer/
#   cable_line already have real setbacks_m entries) — a manhole is an access
#   point on an already-protected line, not an independent structure with its
#   own clearance. Left as "unknown" on purpose, see CLAUDE.md.

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
#
# A second live case (12. Наташинский пр-д, a road-corridor work boundary --
# two rings of 649 and 554 vertices) gaps by 5.0 and 22.9 cm and turned out to
# need more than a bigger number here anyway: `Polygon(points)` on both is
# *invalid* even once closed (a long hand-drafted ring is more likely to
# self-touch by a hair right at its own seam than a simple rectangle is), and
# this function deliberately does not force a repair on an invalid ring --
# see `test_a_self_touching_near_closed_ring_is_left_as_a_line`, a real
# design choice, not an oversight: a silently "fixed" shape nobody checked is
# worse than a clean fallback to a line. The actual fix for that class of gap
# is `reconstruct_closed_footprints()` (already used for buildings) extended
# to `zone_type="territory"` -- it closes rings by noding/snapping the whole
# line network, which handles a self-touching seam correctly (splits it into
# proper simple rings) instead of patching an already-invalid Polygon.
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

    if dxftype == "ARC":
        # Живая находка на «2. Песчаный переулок»: радиусный борт на
        # повороте/перекрёстке (легенда: «радиусный бортовой камень ...
        # внутренний/внешний») бюро рисует отдельными сущностями ARC, не
        # бульжем внутри LWPOLYLINE -- до этой ветки такая дуга была вообще
        # без геометрии (падала в финальный return None), независимо от
        # того, распознан слой или нет. Та же аппроксимация (0.2 м sagitta),
        # что уже принята для HATCH-дуг в _boundary_path_points -- это
        # питает буфер отступа/препятствие, не съёмку, которой нужна точность
        # до миллиметра.
        tool = entity.construction_tool()
        points = [(p.x, p.y) for p in tool.flattening(0.2)]
        if len(points) < 2:
            return None
        return LineString(points)

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

    if dxftype == "HATCH":
        return _hatch_to_geometry(entity)

    if dxftype in ("REGION", "3DSOLID"):
        return _region_to_geometry(entity)

    return None


def _boundary_path_points(path) -> list[tuple[float, float]]:
    """One HATCH boundary loop's vertices, flattened to straight segments.

    A path is either a `PolylinePath` (vertices already flat, `(x, y, bulge)`
    tuples — bulge/arc segments within the polyline are not curved here,
    a small approximation accepted for the same reason the rest of this
    reader accepts one: this feeds an obstacle/fill area, not a surveyed
    boundary that has to be exact to the millimetre) or an `EdgePath` (each
    edge is its own LINE/ARC/ELLIPSE/SPLINE with its own start point; curved
    edges are flattened via ezdxf's own `construction_tool().flattening()`,
    straight ones just contribute their start point — the next edge's start,
    or the loop's own closing point, supplies the rest).
    """
    if hasattr(path, "vertices"):
        return [(v[0], v[1]) for v in path.vertices]

    points: list[tuple[float, float]] = []
    edges = list(getattr(path, "edges", []))
    for edge in edges:
        edge_type = getattr(edge, "EDGE_TYPE", "")
        if edge_type == "LineEdge":
            start = edge.start_point
            points.append((start[0], start[1]))
            continue
        try:
            tool = edge.construction_tool()
            flattened = [(p.x, p.y) for p in tool.flattening(0.2)]
        except Exception:
            start = edge.start_point
            flattened = [(start[0], start[1])]
        points.extend(flattened[:-1] if len(flattened) > 1 else flattened)
    if edges:
        end = edges[-1].end_point
        points.append((end[0], end[1]))
    return points


def _hatch_to_geometry(entity) -> BaseGeometry | None:
    """A HATCH's fill area as shapely geometry -- the actual paved/lawn/tile
    surface polygons a real bureau's drawing carries in a dedicated "fills"
    layer/file, separate from the outline linework `_entity_to_geometry`
    otherwise reads (see geometry_cleanup.reconstruct_closed_footprints's
    docstring for why the outline alone is often not even closed). Until this
    branch existed, HATCH fell through to the final `return None` above --
    not misclassified, not counted as "unknown", just silently absent from
    both utilities and zones.

    Multiple boundary loops on one HATCH are holes-in-an-outer-loop far more
    often than "several unrelated shapes that happen to share a layer" (a
    donut-shaped planter bed cut out of a paved courtyard, say) -- but this
    dataset's fill layers are small per-entity seam/patch hatches (measured:
    tens to a couple hundred m² per entity at most), so treating the largest
    loop as the exterior and subtracting any loop actually contained in it,
    unioning whatever is not contained as separate shapes, covers the real
    shapes without having to trust the DXF boundary-path flag bits (which
    don't reliably distinguish "hole" from "second exterior" across CAD
    export chains).
    """
    loops: list[Polygon] = []
    for path in entity.paths:
        points = _boundary_path_points(path)
        if len(points) < 3:
            continue
        try:
            polygon = Polygon(points)
        except Exception:
            continue
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if polygon.is_empty or polygon.area <= 0:
            continue
        loops.append(polygon)

    if not loops:
        return None
    if len(loops) == 1:
        return loops[0]

    loops.sort(key=lambda p: p.area, reverse=True)
    outer, rest = loops[0], loops[1:]
    holes = [p for p in rest if outer.contains(p)]
    separate = [p for p in rest if p not in holes]

    result = outer
    if holes:
        result = result.difference(unary_union(holes))
    if separate:
        result = unary_union([result, *separate])
    return result if not result.is_empty else None


def _region_to_geometry(entity) -> BaseGeometry | None:
    """A REGION/3DSOLID's flattened 2D footprint -- surface-coverage fills on
    real bureau drawings ("2. Песчаный переулок"'s topography/utility xrefs:
    existing greenery, gas mains, cables, street boundaries, all as REGION,
    not HATCH) that LibreDWG cannot recover at all: verified directly, its
    DWG->DXF conversion writes these entities with a 0-byte ACIS payload
    (`acis_data`/`sab`/`sat` all empty), so the geometry is gone before this
    module ever sees the file, no matter what runs here.

    ODA File Converter (`dwg_convert.py`'s primary backend since organizers
    confirmed in writing there is no ToR restriction on using it for this
    hackathon) preserves the real binary payload -- confirmed directly, not
    assumed: `entity.acis_data` on an ODA-converted REGION starts `b"ASM "`,
    real ShapeManager/ACIS bytes, where the same entity through LibreDWG was
    empty. `ezdxf.acis.api.load_dxf()`/`mesh_from_body()` (an undocumented
    but real, shipped part of ezdxf 1.4.4 -- a full binary ACIS/SAB parser)
    turn that payload into a triangulated mesh; this function reconstructs
    the flat 2D face by building a `Polygon` from each triangle's vertices
    and unioning them per entity. That works regardless of whether a given
    ACIS body triangulates a hole-free face into one loop or several -- no
    triangle is ever emitted over a hole in the first place, so the union
    correctly excludes it without this code having to reconstruct loop
    topology itself, the same reasoning `_hatch_to_geometry` already applies
    to its own boundary loops, just arrived at differently.

    **Verified at real scale, not assumed to work:** every REGION-bearing
    xref file on "2. Песчаный переулок" converts -- 2419/2419 on the busiest
    one (`Красные линии`, red-line markers, ~1ms/entity), 1639 of 1691 (97%)
    across the rest. The failures are ACIS bodies `mesh_from_body` returns
    with zero vertices -- a real limit of what this parser recovers from the
    payload, not a bug in the shapely reconstruction here (their triangle
    loop above never runs, `polygons` stays empty, the entity is skipped the
    same as if it were unreadable, not silently miscounted as covering zero
    area). **A finding that corrected an earlier, wrong guess in this
    project's own history**: it was once assumed REGION entities would need
    a new colour-based classifier because a prior look found them all on
    layer "0" -- that turned out to be an artifact of inspecting an
    unresolved xref block (see `read_dxf_bundle`'s docstring on why xrefs are
    read as separate files, never bound). Read the way this project actually
    reads a bundle -- each xref file on its own -- REGION entities land on
    real, already-mapped layer names (`Леса и газоны` -> existing_greenery,
    `Газопровод`/`Кабели` -> utilities, `Граница улицы` -> road) exactly like
    any other entity type; no new classification logic was needed, only a
    geometry branch that was missing entirely.
    """
    try:
        bodies = ezdxf.acis.api.load_dxf(entity)
    except Exception:
        return None

    polygons: list[Polygon] = []
    for body in bodies:
        try:
            meshes = ezdxf.acis.api.mesh_from_body(body)
        except Exception:
            continue
        for mesh in meshes:
            vertices = mesh.vertices
            for face in mesh.faces:
                if len(face) < 3:
                    continue
                ring = [(vertices[i].x, vertices[i].y) for i in face]
                try:
                    polygon = Polygon(ring)
                except Exception:
                    continue
                if not polygon.is_valid:
                    polygon = polygon.buffer(0)
                if polygon.is_empty or polygon.area <= 0:
                    continue
                polygons.append(polygon)

    if not polygons:
        return None
    return polygons[0] if len(polygons) == 1 else unary_union(polygons)


def iter_entities(
    container,
    explode_blocks: bool = True,
    symbol_layers: frozenset[str] | None = None,
    layer_map: LayerMap | None = None,
    use_layer_rules: bool = False,
) -> Iterator:
    """Yield drawable entities, descending into block references.

    An INSERT on a symbol layer is yielded as-is (its insertion point is the
    feature); any other INSERT is replaced by its contents, recursively.

    `layer_map`, when given, adds a second reason to stop and yield an INSERT
    as-is: its own layer isn't in the map at all. Found live, not guessed --
    a real MGTS (telecom) manhole/well block on "МГТС_ существ. ККС", a layer
    nobody mapped, exploded into 600 raw LINE/SPLINE/ELLIPSE/HATCH primitives
    per instance (18 instances in one 220KB file alone) that all become
    object_type="unknown" anyway -- a type nothing downstream (setbacks,
    zoning_suitability) ever reads. On "1. Олимпийская деревня" this class of
    block accounted for ~205K of 371K layer rows. Every one of those
    primitives, once exploded, was going to resolve to `("zone", "unknown")`
    regardless (`layer_map.get(layer, ("zone", "unknown"))` in read_dxf) --
    the explosion was pure cost with no effect on the object_type it would
    land on. Not exploding it does mean an unmapped composite symbol is
    reported as one "unknown" object instead of hundreds of decorative
    sub-primitives -- coarser, but nothing becomes invisible (the same
    "nothing silently dropped" guarantee this module's docstring makes for
    a single unmapped *leaf* entity, just applied one level higher, to the
    INSERT instead of the primitives it would explode into). A block whose
    own layer *is* mapped (e.g. every "Газопровод" run, itself INSERT-wrapped
    by MicroStation) still explodes exactly as before -- this only short-
    circuits blocks reachable from a layer nothing in the map claims at all.

    `use_layer_rules`, when true, extends "claims" to layer_rules's pattern
    match too -- found live after layer_rules.py landed: a layer like "МГТС_
    существ. ККС" isn't in any literal layer_map, but classify_layer() still
    recognizes it (as a cable run) via its name pattern, so skipping the
    explosion would collapse a real utility down to a single insertion point
    instead of its true geometry. Only genuinely unclassifiable layers (map
    silent AND rules silent) short-circuit when this is on.

    Live data-loss bug, found on "20. Макеева С. ул": the short-circuit above
    only ever checked the INSERT's OWN layer, never what layer the block's
    CONTENT sits on -- and placing a block reference on layer "0" while its
    real content keeps its own layers/colours is ordinary, unremarkable
    AutoCAD practice, not a sign the content is decorative. This street's
    "Здания"/"Части зданий" (building outlines, 219 entities, real polygon
    area once reconstructed) lived inside exactly such a block, referenced
    on layer "0" -- unmapped, unclassifiable by name -- and the short-circuit
    threw the whole building away as a single unclassified insertion point.
    Confirmed directly, not guessed: `building`/`existing_greenery`/
    `power_line_corridor` zones on this street went from 0/0/0 (every one of
    those object types entirely absent, not just under-counted) to 239/13/27
    once block content was actually inspected. `_block_has_classifiable_content()`
    below closes this without giving up the original optimization's real
    win: it inspects each unique block DEFINITION once (memoized by block
    name, recursing into nested INSERTs up to the same depth limit as
    explosion itself), not once per INSTANCE -- the 18-instances-of-one-
    decorative-symbol case this short-circuit was built for still costs one
    inspection, not eighteen, and still short-circuits correctly, because
    that block's content genuinely has nothing classifiable in it either way.
    """
    symbol_layers = SYMBOL_LAYERS if symbol_layers is None else symbol_layers
    block_content_memo: dict[str, bool] = {}

    def _entity_contributes(entity) -> bool:
        """Would this entity, if reached, resolve to something other than
        `object_type="unknown"`? A symbol-layer INSERT is a real single-point
        feature in its own right (existing tree/well/lamp); anything else is
        judged by the same layer_map/layer_rules test `walk()` itself uses."""
        entity_layer = normalize_layer(entity.dxf.layer)
        if entity.dxftype() == "INSERT" and (entity_layer in symbol_layers or is_symbol_layer(entity_layer)):
            return True
        if layer_map is not None and entity_layer in layer_map:
            return True
        return use_layer_rules and classify_layer(entity_layer) is not None

    def _block_has_classifiable_content(entity, depth: int) -> bool:
        if layer_map is None:
            return False
        block_name = entity.dxf.name
        if block_name in block_content_memo:
            return block_content_memo[block_name]
        # Recursion guard first, so a cycle or excessive nesting can't loop
        # forever computing its own memo entry.
        block_content_memo[block_name] = False
        if depth >= _MAX_BLOCK_DEPTH:
            return False
        doc = entity.doc
        block = doc.blocks.get(block_name) if doc is not None else None
        if block is None:
            return False
        found = False
        for child in block:
            if _entity_contributes(child):
                found = True
                break
            if child.dxftype() == "INSERT" and _block_has_classifiable_content(child, depth + 1):
                found = True
                break
        block_content_memo[block_name] = found
        return found

    def walk(entities, depth: int) -> Iterator:
        for entity in entities:
            if entity.dxftype() != "INSERT":
                yield entity
                continue
            layer = normalize_layer(entity.dxf.layer)
            if layer in symbol_layers or is_symbol_layer(layer):
                yield entity
                continue
            if layer_map is not None and layer not in layer_map:
                if (not use_layer_rules or classify_layer(layer) is None) and not _block_has_classifiable_content(
                    entity, depth
                ):
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
    main file, and `ezdxf` does not resolve those (most are DWG, and an attached
    xref is a file reference, not embedded content). Reading the main file alone
    gets the design but no site boundary and no utilities.

    `xref_dirname` pins one folder name; without it every sibling directory
    whose name looks like an xref folder is taken (see XREF_DIR_MARKERS --
    hardcoding "Xrefs" missed most streets, which call it `00_Ссылки`,
    `Внешние ссылки`, `xref_ИТП` and so on). Matches both `.dxf` and `.dwg` in
    the xref folder -- the raw delivery's xrefs are DWG same as the main
    drawing, not pre-converted. `resolve_bundle_inputs` converts whatever this
    returns; this function only locates files.

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
            paths.extend(sorted(p for p in xref_dir.iterdir() if p.suffix.lower() in (".dxf", ".dwg")))
    return [p for p in paths if p.is_file()]


def _bundle_pool_worker_count(n_files: int) -> int:
    """How many workers to convert/read a bundle's files with.

    Was hardcoded to 8 for both the DWG->DXF thread pool and the DXF-read
    process pool. 8 is not an arbitrary number -- it is this project's brief's
    own guaranteed *minimum* target hardware (ТЗ, "Программно-аппаратные
    требования": "не менее 8 логических ядер"), so the hardcode happened to
    exactly match the floor and silently left every core above 8 idle on any
    machine with more. Confirmed live on the dev container (12 cores): both
    pools topped out at ~800% CPU during their busy bursts, nowhere near the
    1200% available.

    `usable` only drops below the machine's full core count once there is
    real headroom above the brief's floor, so an exactly-8-core deployment
    (the guaranteed worst case) keeps today's already-tested full-throttle
    behaviour bit-for-bit, and a beefier box (this dev container, or whatever
    a pilot deployment actually runs on) gets to use the rest instead of
    leaving it idle. One core is held back above that floor for the event
    loop / other concurrent requests -- a background upload should not be
    able to starve the server it is running inside of.
    """
    cores = os.cpu_count() or 1
    usable = cores - 1 if cores > 8 else cores
    return max(1, min(n_files, usable))


def _lpt_partition(paths: list[Path], num_bins: int) -> list[list[Path]]:
    """Greedy longest-processing-time-first bin assignment: sort by file size
    descending, then always add the next file to whichever bin currently has
    the smallest running total size.

    Needed specifically for *batched* conversion (see convert_with_oda_batch),
    where the caller has to decide bin membership up front before dispatch --
    unlike a plain per-file task queue, which a thread/process pool already
    load-balances on its own as workers free up (sorting the queue itself by
    size descending is sufficient there, no explicit bins needed).

    A flat `sorted(..., reverse=True)` guarantees only that the single
    biggest file starts first -- it says nothing about how evenly the *rest*
    divide up. A bundle with one dominant file and many small ones needs that
    file isolated in its own bin from the start, or it ends up sharing a bin
    with several small ones and finishing later than bins that got an even
    spread by luck -- exactly the "~800% CPU falling to ~90% for one file's
    own long tail" trace measured live on a real 33-file bundle (see
    _bundle_pool_worker_count's docstring for the CPU numbers).
    """
    if num_bins <= 1 or len(paths) <= 1:
        return [list(paths)] if paths else []
    ordered = sorted(paths, key=lambda p: p.stat().st_size, reverse=True)
    bins: list[list[Path]] = [[] for _ in range(min(num_bins, len(ordered)))]
    totals = [0] * len(bins)
    for path in ordered:
        target = min(range(len(bins)), key=lambda i: totals[i])
        bins[target].append(path)
        totals[target] += path.stat().st_size
    return bins


class BundleResolutionError(RuntimeError):
    """No drawing found at all, or a DWG needs converting and no backend is
    available. Raised instead of `SystemExit` -- unlike `scripts/plan_dxf.py`'s
    old `_convert_if_needed`, this runs inside a web request too."""


def _default_dwg_convert(path: Path, workdir: Path) -> Path:
    # Imported lazily: dwg_convert shells out to an external binary and has no
    # reason to be on the import path of every caller of this module.
    from geo_engine.io.dwg_convert import available_backend, convert_dwg_to_dxf

    if available_backend() is None:
        raise BundleResolutionError(
            f"Файл {path.name} в формате DWG, но конвертер не найден на PATH. "
            "Установите LibreDWG (dwg2dxf) или ODA File Converter."
        )
    return convert_dwg_to_dxf(path, workdir)


def _other_bundle_members(source: Path, main: Path) -> list[Path]:
    """Every other .dxf/.dwg anywhere under `source` -- not just in a folder
    literally named Xrefs/ссылки.

    Found live, not guessed: "1. Олимпийская деревня"'s project folder keeps
    its utility sheets under per-survey-order subfolders (3ДЖКХ-24_02565/,
    3ДЖКХ-25_03117/, ...) alongside `ссылки/`, not merged into it -- a fixed
    allowlist of folder names (what XREF_DIRNAMES / the old Xrefs-only check
    used) missed all 33 of them and left every object "unknown" (no
    utilities, no territory). Once a caller has already scoped `source` down
    to one project's folder (the directory-input case here always has --
    the ZIP-upload/CLI-directory path, not an unrelated pile of documents),
    everything under it genuinely belongs to that one drawing's xref tree, so
    a full recursive glob is the correct generalisation rather than another
    name to add to an allowlist that will just be incomplete again on street
    #3. Skips `PaxHeader/` -- tar-extraction litter (see the dataset's own
    stray PaxHeader folders), not real bundle content.
    """
    found = set(source.rglob("*.dxf")) | set(source.rglob("*.dwg"))
    found.discard(main)
    return sorted(p for p in found if "PaxHeader" not in p.parts)


def _discover_bundle_members(source: Path) -> tuple[Path, list[Path]]:
    """(main file, every file belonging to the bundle including main), before
    any DWG->DXF conversion.

    Extracted out of what used to be three branches inline in
    `resolve_bundle_inputs()` (a project folder, a lone .dwg, a lone .dxf) so
    `resolve_and_read_bundle()` -- which needs the exact same file selection,
    just without materializing the fully-converted file list before reading
    is allowed to start -- picks files the same way instead of drifting apart
    from this one over time.
    """
    if source.is_dir():
        candidates = sorted(p for p in source.iterdir() if p.suffix.lower() in (".dxf", ".dwg"))
        if not candidates:
            # A real delivery's top level holds the drawing directly -- zero
            # files here usually means this is an umbrella folder one level
            # above the actual project folder (live case: "Исходные данные"
            # for a street holds three unrelated subfolders -- permits,
            # dendrology survey, and the actual "<id>_Генплан... - Standard"
            # drawing set -- none of them at this level). Listing what *is*
            # here turns "no files found" into "look one level down, into one
            # of these" instead of a dead end.
            subdirs = sorted(p.name for p in source.iterdir() if p.is_dir())
            hint = f" Есть подпапки: {', '.join(subdirs)} — чертёж, вероятно, в одной из них." if subdirs else ""
            raise BundleResolutionError(f"В каталоге {source} нет ни одного .dxf/.dwg файла.{hint}")
        # Largest file in the folder root, not by name: real deliveries name
        # sheets after survey order numbers, not "main.dxf".
        main = max(candidates, key=lambda p: p.stat().st_size)
        return main, [main, *_other_bundle_members(source, main)]

    if source.suffix.lower() == ".dwg":
        # The (still unconverted) file has no Xrefs/ of its own to look
        # inside -- look next to the original instead.
        return source, [source, *dxf_bundle_paths(source)[1:]]

    return source, dxf_bundle_paths(source)


def resolve_bundle_inputs(
    source: str | Path,
    workdir: str | Path,
    convert: Callable[[Path, Path], Path] | None = None,
) -> tuple[Path, list[Path], list[str]]:
    """(main drawing, every file in the bundle as DXF, warnings) for a single
    drawing or a project folder, converting DWG on the way in.

    The shared "what is the main drawing, what else belongs with it, convert
    whatever is DWG" step behind both `scripts/plan_dxf.py`/`flatten_bundle.py`
    and the web upload (`backend/app/services/project_service.py`) -- written
    once here so the two do not carry separate copies of "the biggest .dxf/.dwg
    in the folder is the main drawing" and "everything under Xrefs/ belongs to
    it". A caller that wants `SystemExit` instead of `BundleResolutionError`
    (the CLI) catches it at its own boundary; a caller that wants an HTTP 400
    (the web upload) does the same.

    `convert` defaults to `dwg_convert.convert_dwg_to_dxf`; overridable so this
    can be tested without invoking a real converter.
    """
    source = Path(source)
    workdir = Path(workdir)
    convert = convert or _default_dwg_convert
    warnings: list[str] = []

    def convert_if_needed(path: Path) -> Path:
        return convert(path, workdir) if path.suffix.lower() == ".dwg" else path

    def convert_each(paths: list[Path]) -> list[Path]:
        if not paths:
            return []

        # Real, unmocked ODA conversions batch several files into one
        # invocation instead of one call per file -- see
        # convert_with_oda_batch's docstring: measured live, ~7x fewer
        # Qt6+xvfb startups for the same files, because that fixed per-call
        # cost otherwise dominates a small xref file's conversion time. Only
        # taken when using the real default converter (a caller-supplied
        # `convert` override -- tests, mainly -- has no directory-batch
        # equivalent to call) and when ODA is actually the active backend
        # (LibreDWG's dwg2dxf is a cheap native CLI with no comparable
        # per-call GUI-startup tax to amortize, so it keeps the plain
        # per-file path below).
        if convert is _default_dwg_convert:
            from geo_engine.io.dwg_convert import available_backend, convert_with_oda_batch

            if available_backend() == "oda":
                dwg_paths = [p for p in paths if p.suffix.lower() == ".dwg"]
                batch_results: dict[Path, Path] = {}
                if dwg_paths:
                    bins = _lpt_partition(dwg_paths, _bundle_pool_worker_count(len(dwg_paths)))
                    with ThreadPoolExecutor(max_workers=len(bins)) as pool:
                        futures = {pool.submit(convert_with_oda_batch, chunk, workdir): chunk for chunk in bins}
                        for future in futures:
                            chunk = futures[future]
                            try:
                                batch_results.update(future.result())
                            except RuntimeError as error:
                                # The whole bin failed to run at all (see
                                # convert_with_oda_batch's docstring) -- every
                                # file in it stays unconverted, same as any
                                # individual failure below.
                                for path in chunk:
                                    warnings.append(f"пропущен {path.name}: {error}")

                converted = []
                for path in paths:  # canonical order, not bin/dispatch order
                    if path.suffix.lower() != ".dwg":
                        converted.append(path)
                    elif path in batch_results:
                        converted.append(batch_results[path])
                    else:
                        warnings.append(
                            f"пропущен {path.name}: ODA File Converter не создал файл для этого чертежа"
                        )
                return converted

        # Fallback: LibreDWG backend, or a caller-supplied `convert` (tests).
        # No batch API for either, so still one call per file -- but largest
        # files first (a flat descending sort is enough here, unlike the
        # explicit bins above: a thread pool already hands the next queued
        # task to whichever worker frees up first, which for a
        # decreasing-size queue *is* greedy LPT scheduling at runtime) and
        # sized to the machine's real core count, not a hardcoded 8.
        results: dict[Path, Path | RuntimeError] = {}

        def attempt(path: Path) -> None:
            try:
                results[path] = convert_if_needed(path)
            except RuntimeError as error:
                results[path] = error

        ordered = sorted(paths, key=lambda p: p.stat().st_size, reverse=True)
        with ThreadPoolExecutor(max_workers=_bundle_pool_worker_count(len(ordered))) as pool:
            list(pool.map(attempt, ordered))

        converted = []
        for path in paths:  # canonical order, not dispatch order
            outcome = results[path]
            if isinstance(outcome, RuntimeError):
                # One unreadable xref must not sink the whole bundle -- but it
                # must not vanish silently either, or a lost site boundary
                # looks like an absent one. BundleResolutionError is a
                # RuntimeError too, so a missing converter hits this same
                # path for every xref after the first warning.
                warnings.append(f"пропущен {path.name}: {outcome}")
            else:
                converted.append(outcome)
        return converted

    main, all_members = _discover_bundle_members(source)
    converted_main = convert_if_needed(main)
    others = [p for p in all_members if p != main]
    return converted_main, [converted_main, *convert_each(others)], warnings


def _read_one_bundle_file(
    path: Path,
    layer_map: LayerMap | None,
    explode_blocks: bool,
    symbol_layers: frozenset[str] | None,
    drop_origin: bool,
    use_layer_rules: bool,
    reconstruct_footprints: bool = False,
) -> tuple[list[Utility], list[Zone], int]:
    """One file's read_dxf call, module-level and picklable so it can run in
    a worker process (see read_dxf_bundle). Raises straight through --
    deciding a failed file's fate via `on_error` happens in the main
    process, which is iterating futures and already has that callback; a
    worker process has no way to call back into it.

    Opens the document itself (once) and hands it to `read_dxf()` via its
    `doc` parameter, rather than letting `read_dxf()` open it a second time
    -- the entity count this needs for `pick_base_drawing` (see
    `read_dxf_bundle`'s `on_file_read`) comes for free from a document
    that's already sitting in memory in this same worker process, instead of
    the CLI re-reading every bundle file from scratch afterwards purely to
    count entities (measured live, on a real bundle: 16.7s of otherwise
    unexplained CLI time on "4. Харьковская улица", 19 files).
    """
    doc = read_document(path)
    entity_count = len(doc.modelspace())
    utilities, zones = read_dxf(
        path,
        layer_map=layer_map,
        explode_blocks=explode_blocks,
        symbol_layers=symbol_layers,
        stitch_dashes=False,
        drop_origin=drop_origin,
        use_layer_rules=use_layer_rules,
        reconstruct_footprints=reconstruct_footprints,
        doc=doc,
    )
    return utilities, zones, entity_count


def read_dxf_bundle(
    paths: Iterable[str | Path],
    layer_map: LayerMap | None = None,
    explode_blocks: bool = True,
    symbol_layers: frozenset[str] | None = None,
    stitch_dashes: bool = False,
    drop_origin: bool = False,
    use_layer_rules: bool = True,
    reconstruct_footprints: bool = False,
    on_error: Callable[[Path, Exception], None] | None = _warn_unreadable,
    on_file_read: Callable[[Path, int], None] | None = None,
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

    Each file's own read (block explosion, dash-merge-free single-file parse)
    is independent of every other file's -- nothing about it depends on what
    another file in the bundle contains, only the final concatenation does.
    Measured on a real bundle, this step alone was 106.7s of a 122.4s total
    parse, the single biggest chunk of upload time -- so with more than one
    file, this runs across a process pool instead of one file after another.
    Results are collected in the same order `paths` was given (iterating
    `futures` walks it in submission order, not completion order), so the
    combined utilities/zones lists -- and which file's error gets reported
    when several fail -- don't depend on which process happened to finish
    first.

    `on_file_read`, when given, is called once per successfully-read file
    with `(path, entity_count)` -- `entity_count` comes from the same open
    document this read already paid to parse, not a second pass over the
    file. `scripts/plan_dxf.py::main()` uses this to feed `pick_base_drawing`
    without `pick_base_drawing` (or anything else) ever opening these files
    again just to count entities -- that redundant second read used to cost
    16.7s on its own on a real 19-file bundle ("4. Харьковская улица").
    """
    paths = [Path(p) for p in paths]
    utilities: list[Utility] = []
    zones: list[Zone] = []

    if len(paths) > 1:
        worker_count = _bundle_pool_worker_count(len(paths))
        # Largest files first: a size-descending submission queue is greedy
        # LPT scheduling at runtime (the pool hands the next queued task to
        # whichever worker frees up first), so the file that would otherwise
        # be the lone survivor after every other worker has gone idle instead
        # starts immediately alongside everything else. Collection below
        # still walks the ORIGINAL `paths` order, not this one, so the
        # documented "same order paths was given" guarantee is unaffected by
        # how submission was scheduled.
        ordered = sorted(paths, key=lambda p: p.stat().st_size, reverse=True)
        with ProcessPoolExecutor(max_workers=worker_count) as pool:
            future_by_path = {
                path: pool.submit(
                    _read_one_bundle_file,
                    path,
                    layer_map,
                    explode_blocks,
                    symbol_layers,
                    drop_origin,
                    use_layer_rules,
                    reconstruct_footprints,
                )
                for path in ordered
            }
            for path in paths:  # canonical order, not dispatch order
                future = future_by_path[path]
                try:
                    file_utilities, file_zones, entity_count = future.result()
                except Exception as error:  # noqa: BLE001 — судьбу решает вызывающий
                    if on_error is None:
                        raise
                    on_error(path, error)
                    continue
                utilities.extend(file_utilities)
                zones.extend(file_zones)
                if on_file_read is not None:
                    on_file_read(path, entity_count)
    else:
        for path in paths:
            try:
                file_utilities, file_zones, entity_count = _read_one_bundle_file(
                    Path(path),
                    layer_map,
                    explode_blocks,
                    symbol_layers,
                    drop_origin,
                    use_layer_rules,
                    reconstruct_footprints,
                )
            except Exception as error:  # noqa: BLE001 — судьбу решает вызывающий
                if on_error is None:
                    raise
                on_error(Path(path), error)
                continue
            utilities.extend(file_utilities)
            zones.extend(file_zones)
            if on_file_read is not None:
                on_file_read(Path(path), entity_count)

    if stitch_dashes:
        utilities = stitch_utility_lines(utilities)
    if reconstruct_footprints:
        # Тот же приём, что уже применяется для stitch_dashes выше: то, что
        # действительно кроссфайловое, не может быть сделано внутри
        # per-file read_dxf() (тот видит только один файл бандла за раз).
        # `read_dxf()`'s собственный вызов _add_closed_road_polygons() уже
        # замкнул петли борта, целиком лежащие в одном xref-файле съёмки —
        # этот проход по объединённому списку зон дополнительно замыкает
        # петли, разрезанные по границе файлов (см. функции докстринг).
        zones = _add_closed_road_polygons(zones)
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


# Zone types reconstructed from line soup into closed polygons when
# `reconstruct_footprints=True` — see geometry_cleanup.reconstruct_closed_footprints
# for why this is necessary at all. Keyed by zone_type -> (snap_grid_m,
# dangle_buffer_m), not a flat list, because the two live cases need
# different values for BOTH:
#
# * snap grid: a building footprint's drafted gaps are metres (checked live
#   on the pilot data, real "Здания" layer, 0/409 LWPOLYLINE entities closed;
#   DEFAULT_SNAP_GRID_M's 5 cm is already generous there), but a long
#   hand-drafted work-area boundary (hundreds of vertices tracing a real road
#   corridor — 12. Наташинский пр-д) gaps by up to 23 cm at its own closing
#   seam, and is invalid as a naive closed Polygon even once that gap is
#   bridged (self-touches itself by a hair right at the seam — the same
#   class of ring `_entity_to_geometry`'s own close-tolerance deliberately
#   does not force-repair, see `_POLYLINE_CLOSE_TOLERANCE_M`'s comment).
#   A second, wider live gap moved this again: «1. Олимпийская деревня»'s
#   own outer "Границы работ" is drawn as two separate open polylines meant
#   to close against EACH OTHER, not each against itself — one endpoint
#   pair is 6 cm apart, the other 1.09 m, past what 0.3 m bridges. Widening
#   further is not simply "safer the bigger" — measured directly (see
#   worklog): `shapely.set_precision()`'s grid snap is position-dependent,
#   not a clean "any grid >= the gap works" — scanning 1.1-6.0 m in 0.1 m
#   steps found the correct 9-piece result (8 known small rings + this one
#   big outer one, nothing spuriously split) at exactly 2.0 m, with several
#   nearby values (2.1, 3.0, 3.2...) failing to close it at all and several
#   larger ones introducing NEW spurious splits elsewhere on the same
#   network. 2.0 m is therefore not a "round up for safety margin" choice —
#   it is the measured value that actually reproduces the real boundary
#   without side effects, same empirical spirit as every other constant in
#   this function's docstring.
# * dangle buffer: for a building, a fragment that can't be closed at all is
#   still kept as a thin buffered sliver — an imperfect obstacle beats a
#   vanished one (see reconstruct_closed_footprints' own docstring). For
#   territory that trade reverses: a thin sliver silently standing in for
#   "the whole legal work area" is worse than the loud MissingTerritoryError
#   a genuinely absent boundary should raise (live case, 7. Нижние Поля ул:
#   the only candidate on the mapped layer is an isolated 9.4 m stub with no
#   partner anywhere in the bundle — buffering it would manufacture a ~6 m²
#   "territory" out of a street that in truth has none in this input at
#   all). `dangle_buffer_m=0.0` makes every unclosable leftover buffer to an
#   empty geometry (`leftover.buffer(0)`) and contribute nothing, so only
#   genuinely closed rings count.
#
# Both have a real correctness consequence if left as lines: `buildable_area`'s
# hard-obstacle subtraction is a no-op on a LineString, and territory_polygon
# either misses a real work area entirely or silently keeps only whatever
# small fragments elsewhere on the layer happen to already close on their
# own. "road" stays a deliberate line (the kerb, not the carriageway — see
# MOSGEOTREST_LAYER_MAP's "Бортовой камень" comment), so it is not in here.
#
# "existing_greenery" joined the same list for the same reason as
# "building", not "territory": the real-data finding (4. Харьковская улица)
# was that "Полоса деревьев"/"Леса и газоны" arrive as boundary LineStrings
# around a green area (92% of the layer's objects on that street), not
# filled polygons — the map rendered them as a scatter of thin lines/dots
# instead of a filled patch, and `buildable_area` was silently not
# subtracting them at all (a LineString has zero area to subtract). It's
# also a hard obstacle in the exact same sense as a building (real existing
# vegetation, not a soft scoring factor — see buffers.HARD_OBSTACLE_ZONE_TYPES),
# so the same building-style tradeoff applies: an imperfectly-closed
# dangle-buffered sliver is still a real (if approximate) obstacle, and
# safer than one that silently vanishes because reconstruct_closed_footprints
# couldn't fully close a hand-drafted line into a valid ring —
# `dangle_buffer_m=DEFAULT_DANGLE_BUFFER_M` (not territory's 0.0).
RECONSTRUCT_FOOTPRINT_ZONE_TYPES: dict[str, tuple[float, float]] = {
    "building": (DEFAULT_SNAP_GRID_M, DEFAULT_DANGLE_BUFFER_M),
    "territory": (2.0, 0.0),
    "existing_greenery": (DEFAULT_SNAP_GRID_M, DEFAULT_DANGLE_BUFFER_M),
}

ADDITIVE_ROAD_POLYGON_ZONE_TYPES = ("road", "sidewalk")


def _add_closed_road_polygons(zones: list[Zone]) -> list[Zone]:
    """Живая находка на «2. Песчаный переулок»: road/sidewalk уже в
    buffers.HARD_OBSTACLE_ZONE_TYPES, но это был no-op с самого начала
    проекта -- ни у того, ни у другого никогда не было Polygon-геометрии
    для фильтра. В отличие от building/territory/existing_greenery выше,
    здесь линии НЕЛЬЗЯ заменить реконструкцией целиком: у бортовой линии
    есть законные незамкнутые концы (дорога продолжается за границей
    съёмки), и буферизовать их в тонкий срез было бы неверно -- уже
    работающий путь линия+отступ корректно защищает именно эти участки.
    Поэтому это отдельный, аддитивный шаг: существующие zone (линии) не
    трогает, только добавляет новые Polygon-зоны там, где сеть борта
    реально замкнулась в контур. `reconstruct_closed_road_polygons()`
    тихо отбрасывает всё незамкнутое вместо буферизации в срез -- см. её
    докстринг.

    Вызывается дважды, не один раз: изнутри `read_dxf()` (замыкает петли
    борта, целиком лежащие в одном файле) и ещё раз изнутри
    `read_dxf_bundle()` на уже объединённом списке зон всех файлов пачки
    (замыкает петли, разрезанные по границе файлов съёмки). Дублирования
    не возникает: то, что уже замкнулось на первом проходе, стало
    Polygon и не попадает в `targeted` фильтр по `geom_type` второго
    прохода -- обрабатываются только оставшиеся LineString/
    MultiLineString. Живой результат на Песчаном: 23 полигона (4583 м²)
    только внутрифайловым проходом -> 49 полигонов (8460 м²) после
    добавления прохода на объединённом бандле -- почти половина реальных
    петель борта разрезана по границам xref-файлов съёмки.
    """
    for road_type in ADDITIVE_ROAD_POLYGON_ZONE_TYPES:
        targeted = [
            z for z in zones
            if z.zone_type == road_type and z.geometry.geom_type in ("LineString", "MultiLineString")
        ]
        if not targeted:
            continue
        extra_polygons = reconstruct_closed_road_polygons([z.geometry for z in targeted])
        if extra_polygons:
            layer_note = targeted[0].attrs or {}
            zones.extend(Zone(geometry=g, zone_type=road_type, attrs=layer_note) for g in extra_polygons)
    return zones


def resolve_and_read_bundle(
    source: str | Path,
    workdir: str | Path,
    layer_map: LayerMap | None = None,
    convert: Callable[[Path, Path], Path] | None = None,
    explode_blocks: bool = True,
    symbol_layers: frozenset[str] | None = None,
    stitch_dashes: bool = False,
    drop_origin: bool = False,
    use_layer_rules: bool = True,
    reconstruct_footprints: bool = False,
    on_error: Callable[[Path, Exception], None] | None = _warn_unreadable,
) -> tuple[list[Utility], list[Zone], list[str]]:
    """`resolve_bundle_inputs()` followed by `read_dxf_bundle()`, overlapped
    instead of sequential.

    Measured live on a real 33-file/52MB bundle ("1. Олимпийская деревня"):
    conversion (25.3s -- an external GUI subprocess, ODA under xvfb, mostly
    waiting on its own startup and the OS) and reading (94.7s -- real
    Python/shapely CPU work) are two fully independent resources that
    `resolve_bundle_inputs()` followed by `read_dxf_bundle()` nonetheless
    pays for back to back (120s total), because the first function cannot
    return anything until *every* file has finished converting. Nothing
    stops file 1's read from starting the moment file 1 finishes converting
    while file 30 is still being converted -- this function does exactly
    that: a bundle member that is already `.dxf` needs no conversion and is
    handed to the read pool immediately; each `.dwg` is handed off to it the
    moment its own conversion completes (`as_completed`, not "after the
    whole batch"). In the best case this brings the wall-clock for the two
    stages together down towards `max(convert, read)` instead of their sum.

    Result order is still deterministic and independent of which file
    happens to finish first -- the same guarantee `read_dxf_bundle` documents
    for itself -- because results are collected keyed by source path and only
    concatenated in `_discover_bundle_members`'s canonical order at the end,
    never in completion order.

    The main file's own conversion is a hard failure (propagates), same as
    `resolve_bundle_inputs`; every other bundle member's conversion failure
    is a soft warning (collected and returned, not raised), also matching
    `resolve_bundle_inputs` -- one bad xref must not sink the whole street.
    Read failures go through `on_error`, matching `read_dxf_bundle`.

    Used by the web upload path (`project_service.py`), which has no need for
    "which file was main" once reading is done (`_` already discarded it
    before this function existed) -- unlike `resolve_bundle_inputs`, this one
    does not return the main file's path. `scripts/plan_dxf.py` keeps its own
    separate resolution (see `resolve_bundle_inputs`'s docstring) and does not
    use this function -- it still needs `pick_base_drawing()`'s per-file
    entity counts, which this function does not expose.
    """
    source = Path(source)
    workdir = Path(workdir)
    convert = convert or _default_dwg_convert
    warnings: list[str] = []

    def convert_if_needed(path: Path) -> Path:
        return convert(path, workdir) if path.suffix.lower() == ".dwg" else path

    main, all_members = _discover_bundle_members(source)
    converted_main = convert_if_needed(main)  # hard failure, same as resolve_bundle_inputs
    others = [p for p in all_members if p != main]

    def submit_read(pool: ProcessPoolExecutor, dxf_path: Path) -> Future:
        return pool.submit(
            _read_one_bundle_file,
            dxf_path,
            layer_map,
            explode_blocks,
            symbol_layers,
            drop_origin,
            use_layer_rules,
            reconstruct_footprints,
        )

    utilities: list[Utility] = []
    zones: list[Zone] = []

    with ProcessPoolExecutor(max_workers=_bundle_pool_worker_count(len(all_members))) as read_pool:
        # The main file needs no waiting -- already converted above -- so its
        # read starts immediately, overlapped with every other file below
        # instead of just being "first in line" the way the old sequential
        # resolve-then-read left it.
        pending: dict[Path, Future] = {main: submit_read(read_pool, converted_main)}

        dwg_others = [p for p in others if p.suffix.lower() == ".dwg"]
        already_dxf = [p for p in others if p.suffix.lower() != ".dwg"]
        for path in already_dxf:
            pending[path] = submit_read(read_pool, path)

        if dwg_others:
            # Largest first (LPT), same reasoning as resolve_bundle_inputs's
            # convert_each fallback path -- a decreasing-size submission
            # queue is greedy LPT scheduling at runtime for a plain
            # thread-per-file pool. Deliberately NOT the batched-ODA path
            # convert_each uses: batching would delay *every* file in a bin
            # until the whole bin's ODA call returns, which defeats the
            # entire point here of starting each file's read the moment
            # *that* file's own conversion is done.
            ordered = sorted(dwg_others, key=lambda p: p.stat().st_size, reverse=True)
            with ThreadPoolExecutor(max_workers=_bundle_pool_worker_count(len(ordered))) as convert_pool:
                convert_futures = {convert_pool.submit(convert_if_needed, path): path for path in ordered}
                for future in as_completed(convert_futures):
                    original = convert_futures[future]
                    try:
                        dxf_path = future.result()
                    except RuntimeError as error:
                        warnings.append(f"пропущен {original.name}: {error}")
                        continue
                    pending[original] = submit_read(read_pool, dxf_path)

        for path in all_members:  # canonical order, not completion order
            future = pending.get(path)
            if future is None:
                continue  # conversion failed and was already warned about above
            try:
                file_utilities, file_zones, _entity_count = future.result()
            except Exception as error:  # noqa: BLE001 — судьбу решает вызывающий
                if on_error is None:
                    raise
                on_error(path, error)
                continue
            utilities.extend(file_utilities)
            zones.extend(file_zones)

    if stitch_dashes:
        utilities = stitch_utility_lines(utilities)
    if reconstruct_footprints:
        zones = _add_closed_road_polygons(zones)

    return utilities, zones, warnings


def read_dxf(
    path: str | Path,
    layer_map: LayerMap | None = None,
    explode_blocks: bool = True,
    symbol_layers: frozenset[str] | None = None,
    stitch_dashes: bool = False,
    drop_origin: bool = False,
    use_layer_rules: bool = True,
    reconstruct_footprints: bool = False,
    doc=None,
) -> tuple[list[Utility], list[Zone]]:
    """Parse a DXF file's modelspace into Utility and Zone lists, keyed by
    layer name via `layer_map` (defaults to DEFAULT_LAYER_MAP).

    `stitch_dashes`, `drop_origin` and `reconstruct_footprints` are the
    CAD-export cleanups described in geometry_cleanup — off by default
    because they only apply to drawings that have those artifacts, and a
    caller reading a clean DXF should get exactly what the file contains.

    `use_layer_rules` включает распознавание слоя по образцу имени, когда
    дословной записи в карте нет (см. layer_rules). По умолчанию включено:
    без него читались семь улиц пилота из девятнадцати.

    `doc`, when given, is an already-opened ezdxf document to read instead of
    opening `path` again -- `_read_one_bundle_file()` uses this to count a
    bundle file's entities (for `pick_base_drawing`) from the same open
    document this function would otherwise open a second time for.
    """
    layer_map = layer_map or DEFAULT_LAYER_MAP
    if doc is None:
        doc = read_document(Path(path))
    msp = doc.modelspace()

    utilities: list[Utility] = []
    zones: list[Zone] = []

    for entity in iter_entities(
        msp,
        explode_blocks=explode_blocks,
        symbol_layers=symbol_layers,
        layer_map=layer_map,
        use_layer_rules=use_layer_rules,
    ):
        geometry = _entity_to_geometry(entity)
        if geometry is None or geometry.is_empty:
            continue

        layer = normalize_layer(entity.dxf.layer)
        kind, object_type = resolve_layer(layer, layer_map, use_layer_rules)

        if kind == "utility":
            utilities.append(Utility(geometry=geometry, object_type=object_type, layer_source=layer))
        else:
            zones.append(Zone(geometry=geometry, zone_type=object_type, attrs={"layer": layer}))

    if reconstruct_footprints:
        # Per zone_type, not globally: pooling e.g. building outlines with
        # unrelated road-edge lines into one polygonize() call would let GEOS
        # node them together at any incidental shared point and merge two
        # unrelated objects into one bogus ring.
        for footprint_type, (snap_grid_m, dangle_buffer_m) in RECONSTRUCT_FOOTPRINT_ZONE_TYPES.items():
            targeted = [z for z in zones if z.zone_type == footprint_type]
            if not targeted:
                continue
            rest = [z for z in zones if z.zone_type != footprint_type]
            # Point geometry -- existing_greenery's individual tree markers
            # ("Отдельно стоящее дерево") are the live case -- has nothing to
            # close into a ring and isn't line-soup either;
            # reconstruct_closed_footprints() has no branch for it and would
            # silently drop it if handed in, which for a real existing tree is
            # a correctness regression (a new candidate could then legally
            # land right on top of it), not just a cosmetic loss. Passed
            # through untouched instead, same as before reconstruction
            # existed for this zone_type at all.
            points = [z for z in targeted if z.geometry.geom_type == "Point"]
            linelike = [z for z in targeted if z.geometry.geom_type != "Point"]
            rebuilt = reconstruct_closed_footprints(
                [z.geometry for z in linelike], dangle_buffer_m=dangle_buffer_m, snap_grid_m=snap_grid_m
            )
            layer_note = (linelike[0].attrs if linelike else points[0].attrs) or {}
            zones = rest + points + [Zone(geometry=g, zone_type=footprint_type, attrs=layer_note) for g in rebuilt]

        zones = _add_closed_road_polygons(zones)

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
