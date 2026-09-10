import ezdxf
from shapely.geometry import Point, Polygon

from geo_engine.io.dxf_writer import write_dxf
from geo_engine.model import PlantingItem


def test_dxf_export_round_trips_geometry_and_layers(tmp_path):
    items = [
        PlantingItem(geometry=Point(10, 20), planting_type="tree", species="default", score=0.9, rationale="r"),
        PlantingItem(geometry=Point(15, 20), planting_type="shrub", species="default", score=0.7, rationale="r"),
        PlantingItem(
            geometry=Polygon([(0, 0), (5, 0), (5, 5), (0, 5)]),
            planting_type="lawn",
            species="default",
            score=0.5,
            rationale="r",
        ),
    ]

    out_path = tmp_path / "plan.dxf"
    write_dxf(items, out_path)
    assert out_path.exists()

    doc = ezdxf.readfile(str(out_path))
    msp = doc.modelspace()
    layers_used = {e.dxf.layer for e in msp}

    assert {"TREES", "SHRUBS", "LAWN"} <= layers_used
    circles = [e for e in msp if e.dxftype() == "CIRCLE"]
    polylines = [e for e in msp if e.dxftype() == "LWPOLYLINE"]
    assert len(circles) == 2  # tree + shrub
    assert len(polylines) == 1  # lawn
