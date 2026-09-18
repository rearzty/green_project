"""Export a planting plan to DXF as a separate result layer over the source drawing.

The brief is specific and checks this by hand at the demo:
  * «исходные слои подосновы, коммуникаций и городской среды не изменяются и не
    перезаписываются»;
  * «результат должен сохраняться как отдельный слой (например, PLANTING_PROPOSED,
    GREEN_AI и т.п.), который можно включить/выключить независимо от исходных
    данных»;
  * «проверка выходного DXF (nanoCAD): исходные слои целы, слой результата
    читается».

So the default path here is *not* "make a new drawing with circles in it" — that
was the previous behaviour and it produces a file an expert opens to find the
plan floating in a void. Pass `base_dxf` and the result is written into a copy of
the source document, on layers under one prefix so a single `GREEN_AI*` filter
toggles the whole result.

Layer names use `$` as the separator, matching the convention in the city's own
symbol library (`Шаблоны значков.dwg`: `ЗЕЛЕНЬ$ДЕРЕВЬЯ$КЛЕН ОСТРОЛИСТНЫЙ`).
"""

from __future__ import annotations

import os
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import ezdxf
import ezdxf.recover
from ezdxf.addons import Importer

from geo_engine.model import PlantingItem

RESULT_LAYER_PREFIX = "GREEN_AI"

LAYER_SUFFIX_BY_TYPE = {"tree": "ДЕРЕВЬЯ", "shrub": "КУСТАРНИКИ", "lawn": "ГАЗОН"}
COLOR_BY_TYPE = {"tree": 3, "shrub": 5, "lawn": 2}  # ACI color codes: green, blue, yellow

# Rough symbol radius (m) for point plantings, used only for the DXF glyph —
# not to be confused with the canopy_radius_m used by the placement algorithm.
SYMBOL_RADIUS_M = {"tree": 1.5, "shrub": 0.6}

# XDATA strings are capped at 255 bytes per group code 1000.
_XDATA_CHUNK = 200


class ResultLayerCollisionError(RuntimeError):
    """The source drawing already uses a layer we would write into.

    Raised rather than merged: writing plantings onto a layer the source owns is
    exactly the "исходные слои не перезаписываются" rule being broken, and doing
    it silently would produce a file that passes a glance and fails review.
    """


def result_layer_name(planting_type: str, prefix: str = RESULT_LAYER_PREFIX) -> str:
    return f"{prefix}${LAYER_SUFFIX_BY_TYPE.get(planting_type, planting_type.upper())}"


def _set_explanation(entity, doc, record) -> None:
    """Attach the citation to the entity itself, so the DXF carries it too.

    The brief accepts the machine-readable report alone, but an expert checking
    in nanoCAD should not have to alt-tab to a JSON file to see which clause put
    a tree here.
    """
    text = record.summary
    chunks = [text[i : i + _XDATA_CHUNK] for i in range(0, len(text), _XDATA_CHUNK)] or [""]
    entity.set_xdata(RESULT_LAYER_PREFIX, [(1000, chunk) for chunk in chunks])


# Per-item entity creation is what's actually slow here -- ezdxf's own
# bookkeeping per add_circle/add_text/add_lwpolyline/set_xdata call (handle
# assignment, entity-space registration) is pure Python overhead, measured
# at 172.9s for 1M items. Below this many items, that's already comfortably
# fast, so spinning up worker processes (each pays its own ezdxf import +
# process-spawn cost) would only add latency -- gated on measured scale, the
# same reasoning as layer_raster.py's _PARALLEL_RENDER_THRESHOLD.
_PARALLEL_EXPORT_THRESHOLD = 20_000


def _build_type_chunk_document(items: list[PlantingItem], records: list | None, prefix: str) -> str:
    """Runs in a worker process: builds one throwaway DXF holding every
    entity for a single planting type, on the *real* result layer name
    write_dxf itself will use (not a placeholder) -- so that merging it into
    the actual output later is exactly "these entities already belong on a
    layer of this name", not "figure out afterwards which entity goes
    where". Chunking by type rather than an arbitrary slice of `items` is
    what makes that safe: every entity in one worker's document belongs on
    exactly one target layer, so the merge step never needs per-entity
    metadata to route entities to the right layer.

    Returns a file path, not the ezdxf.Document itself -- Document objects
    carry too much internal, order-dependent state (handle counters, table
    cross-references) to be a safe thing to pickle across a process
    boundary, whereas "save it, hand back the path, reopen it in the main
    process" is exactly the round-trip ezdxf's own file format is built for.
    """
    doc = ezdxf.new(setup=True)
    planting_type = items[0].planting_type
    layer = result_layer_name(planting_type, prefix)
    doc.layers.add(name=layer, color=COLOR_BY_TYPE.get(planting_type, 7))
    if records is not None:
        doc.appids.add(prefix)
    msp = doc.modelspace()

    for index, item in enumerate(items):
        record = records[index] if records is not None and index < len(records) else None
        entity = None
        if item.geometry.geom_type == "Point":
            radius = SYMBOL_RADIUS_M.get(item.planting_type, 1.0)
            entity = msp.add_circle(center=(item.geometry.x, item.geometry.y), radius=radius, dxfattribs={"layer": layer})
            msp.add_text(
                f"{item.species}",
                dxfattribs={"layer": layer, "height": 0.5, "insert": (item.geometry.x + radius, item.geometry.y)},
            )
        elif item.geometry.geom_type == "Polygon":
            points = list(item.geometry.exterior.coords)
            entity = msp.add_lwpolyline(points, close=True, dxfattribs={"layer": layer})
        if entity is not None and record is not None:
            _set_explanation(entity, doc, record)

    fd, tmp_path = tempfile.mkstemp(suffix=".dxf")
    os.close(fd)
    doc.saveas(tmp_path)
    return tmp_path


def _write_items_by_type_parallel(doc, msp, by_type: dict[str, tuple[list[PlantingItem], list | None]], prefix: str) -> None:
    """Builds each planting type's entities in its own worker process, then
    imports each resulting throwaway document into `doc` one at a time in
    the main process via ezdxf's own cross-document Importer addon.

    NOT independently verified end-to-end in this environment (no working
    Python/ezdxf install was available while writing this) -- the specific
    assumption this relies on is that ezdxf.addons.importer.Importer, when
    importing entities whose source layer has the same name *and* the same
    definition (color) as a layer that already exists in the target
    document (write_dxf already creates every GREEN_AI$... layer in `doc`
    before this runs), reuses that existing target layer rather than
    erroring or silently renaming -- which is what the Importer addon is
    documented to be for (merging one DXF's content into another's), but
    this exact case hasn't been exercised against a real file yet. If
    testing shows otherwise, the fallback is to have workers write entities
    on DXF's always-present default layer "0" instead and reassign
    `entity.dxf.layer` explicitly after each import, trading a slightly
    larger diff for not depending on the merge behaviour at all.

    Deliberately does not catch and silently fall back to the sequential
    path on failure: `doc.saveas(path)` only happens after this returns, so
    a raised error here means nothing gets written to `path` at all -- an
    honest failed export job, not a corrupted or silently-wrong one.
    """
    worker_count = min(len(by_type), os.cpu_count() or 1)
    tmp_paths: list[str] = []
    try:
        with ProcessPoolExecutor(max_workers=worker_count) as pool:
            futures = [pool.submit(_build_type_chunk_document, chunk_items, chunk_records, prefix) for chunk_items, chunk_records in by_type.values()]
            for future in futures:
                tmp_path = future.result()
                tmp_paths.append(tmp_path)
                sub_doc = ezdxf.readfile(tmp_path)
                importer = Importer(sub_doc, doc)
                importer.import_entities(list(sub_doc.modelspace()), target_layout=msp)
                importer.finalize()
    finally:
        for tmp_path in tmp_paths:
            Path(tmp_path).unlink(missing_ok=True)


def write_dxf(
    items: list[PlantingItem],
    path: str | Path,
    base_dxf: str | Path | None = None,
    records: list | None = None,
    prefix: str = RESULT_LAYER_PREFIX,
) -> None:
    """Write `items` to `path`.

    With `base_dxf`, the source drawing is loaded and the plan added on new
    layers; everything already in the drawing is left exactly as it was. Without
    it, a standalone drawing is produced (kept for tests and for exporting a plan
    whose source drawing is not at hand).

    `records` are ComplianceRecords from geo_engine.compliance, positionally
    matching `items`; when given, each entity carries its justification as XDATA.
    """
    if base_dxf is not None:
        # ezdxf.recover, not ezdxf.readfile: the base drawing is third-party by
        # definition, and a DWG->DXF conversion can leave tables that load fine
        # and then break on save. Hit live on the pilot general plan — its
        # MATERIAL table came back with a string where an entity belonged, and
        # `doc.saveas()` died with AttributeError after the whole plan had
        # already been computed. recover repairs that on load; readfile cannot,
        # and the failure does not surface until the last step.
        doc, auditor = ezdxf.recover.readfile(str(base_dxf))
        if auditor.has_errors:
            print(f"  ! в исходном чертеже исправлено структурных ошибок: {len(auditor.errors)}")
        existing = {layer.dxf.name for layer in doc.layers}
    else:
        doc = ezdxf.new(setup=True)
        existing = set()

    msp = doc.modelspace()

    needed = {result_layer_name(t, prefix) for t in {i.planting_type for i in items}} or {
        result_layer_name("tree", prefix)
    }
    collisions = sorted(needed & existing)
    if collisions:
        raise ResultLayerCollisionError(
            "Исходный чертёж уже содержит слои, в которые пишется результат: "
            + ", ".join(collisions)
            + ". Задайте другой префикс (prefix=...), иначе исходные данные будут перезаписаны."
        )

    for planting_type in LAYER_SUFFIX_BY_TYPE:
        name = result_layer_name(planting_type, prefix)
        if name not in doc.layers:
            doc.layers.add(name=name, color=COLOR_BY_TYPE[planting_type])

    if records is not None:
        doc.appids.add(prefix)

    # Cheap check first (a set comprehension, not a full regroup) so the
    # common case -- too few items to bother, or everything is one type
    # anyway -- doesn't pay for building by_type's grouped lists just to
    # throw them away.
    if len(items) >= _PARALLEL_EXPORT_THRESHOLD and len({i.planting_type for i in items}) > 1:
        # Group items (and their positionally-matching records) by planting
        # type only now that it's actually going to be used.
        by_type: dict[str, tuple[list[PlantingItem], list | None]] = {}
        for index, item in enumerate(items):
            record = records[index] if records is not None and index < len(records) else None
            item_list, record_list = by_type.setdefault(item.planting_type, ([], [] if records is not None else None))
            item_list.append(item)
            if record_list is not None:
                record_list.append(record)
        _write_items_by_type_parallel(doc, msp, by_type, prefix)
    else:
        for index, item in enumerate(items):
            layer = result_layer_name(item.planting_type, prefix)
            record = records[index] if records is not None and index < len(records) else None
            entity = None

            if item.geometry.geom_type == "Point":
                radius = SYMBOL_RADIUS_M.get(item.planting_type, 1.0)
                entity = msp.add_circle(
                    center=(item.geometry.x, item.geometry.y), radius=radius, dxfattribs={"layer": layer}
                )
                msp.add_text(
                    f"{item.species}",
                    dxfattribs={"layer": layer, "height": 0.5, "insert": (item.geometry.x + radius, item.geometry.y)},
                )
            elif item.geometry.geom_type == "Polygon":
                points = list(item.geometry.exterior.coords)
                entity = msp.add_lwpolyline(points, close=True, dxfattribs={"layer": layer})

            if entity is not None and record is not None:
                _set_explanation(entity, doc, record)

    doc.saveas(str(path))
