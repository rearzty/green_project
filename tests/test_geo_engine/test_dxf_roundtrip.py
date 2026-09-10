import ezdxf
from shapely.geometry import Point, Polygon

from geo_engine.io.dxf_reader import DEFAULT_LAYER_MAP, read_dxf
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


def test_dxf_boundary_layer_imports_as_territory_zone(tmp_path):
    """pipeline_service._territory_polygon() looks up zone_type == "territory"
    to find the site outline — same contract the GeoJSON/SHP reader and
    generate_synthetic_data.py already follow. The DXF BOUNDARY layer must
    map to that same zone_type, or a DXF-imported project can never generate
    a plan (regression test for the bug where it mapped to "boundary" instead).
    """
    doc = ezdxf.new(setup=True)
    for layer_name in DEFAULT_LAYER_MAP:
        doc.layers.add(name=layer_name)
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (100, 0), (100, 80), (0, 80)], close=True, dxfattribs={"layer": "BOUNDARY"})

    dxf_path = tmp_path / "territory.dxf"
    doc.saveas(str(dxf_path))

    _, zones = read_dxf(dxf_path)

    assert any(z.zone_type == "territory" for z in zones)
