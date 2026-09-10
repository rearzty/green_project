"""Export a final planting plan back to DXF, one layer per planting type."""

from __future__ import annotations

from pathlib import Path

import ezdxf

from geo_engine.model import PlantingItem

LAYER_BY_TYPE = {"tree": "TREES", "shrub": "SHRUBS", "lawn": "LAWN"}
COLOR_BY_TYPE = {"tree": 3, "shrub": 5, "lawn": 2}  # ACI color codes: green, blue, yellow

# Rough symbol radius (m) for point plantings, used only for the DXF glyph —
# not to be confused with the canopy_radius_m used by the placement algorithm.
SYMBOL_RADIUS_M = {"tree": 1.5, "shrub": 0.6}


def write_dxf(items: list[PlantingItem], path: str | Path) -> None:
    doc = ezdxf.new(setup=True)
    msp = doc.modelspace()

    for planting_type, layer_name in LAYER_BY_TYPE.items():
        if layer_name not in doc.layers:
            doc.layers.add(name=layer_name, color=COLOR_BY_TYPE[planting_type])

    for item in items:
        layer = LAYER_BY_TYPE.get(item.planting_type, "0")

        if item.geometry.geom_type == "Point":
            radius = SYMBOL_RADIUS_M.get(item.planting_type, 1.0)
            msp.add_circle(center=(item.geometry.x, item.geometry.y), radius=radius, dxfattribs={"layer": layer})
            msp.add_text(
                f"{item.species}",
                dxfattribs={"layer": layer, "height": 0.5, "insert": (item.geometry.x + radius, item.geometry.y)},
            )
        elif item.geometry.geom_type == "Polygon":
            points = list(item.geometry.exterior.coords)
            msp.add_lwpolyline(points, close=True, dxfattribs={"layer": layer})
        # rationale/score are not directly representable in plain DXF entities;
        # they live in the plan JSON/DB — DXF is the CAD hand-off artifact only.

    doc.saveas(str(path))
