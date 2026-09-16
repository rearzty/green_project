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

from pathlib import Path

import ezdxf
import ezdxf.recover

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
