import ezdxf
import pytest
from shapely.geometry import LineString, Point, Polygon

from geo_engine.io.dxf_reader import DEFAULT_LAYER_MAP, read_dxf
from geo_engine.io.dxf_writer import (
    RESULT_LAYER_PREFIX,
    ResultLayerCollisionError,
    result_layer_name,
    write_dxf,
)
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

    assert {result_layer_name(t) for t in ("tree", "shrub", "lawn")} <= layers_used
    assert all(name.startswith(f"{RESULT_LAYER_PREFIX}$") for name in layers_used if name != "0")
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


def _source_drawing(tmp_path):
    """A stand-in for a real geobase sheet: two source layers with content."""
    doc = ezdxf.new(setup=True)
    doc.layers.add(name="Газопровод")
    doc.layers.add(name="Здания")
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (100, 0)], dxfattribs={"layer": "Газопровод"})
    msp.add_lwpolyline([(10, 20), (30, 20), (30, 40), (10, 40)], close=True, dxfattribs={"layer": "Здания"})
    path = tmp_path / "source.dxf"
    doc.saveas(str(path))
    return path


def test_result_is_written_over_the_source_without_touching_it(tmp_path):
    """The brief checks this by hand at the demo: open the output in nanoCAD,
    the source layers must be intact and the result must be a layer you can
    switch off. Writing a fresh drawing with only circles in it fails that.
    """
    source = _source_drawing(tmp_path)
    before = ezdxf.readfile(str(source))
    source_layers = {layer.dxf.name for layer in before.layers}
    source_entities = [(e.dxftype(), e.dxf.layer) for e in before.modelspace()]

    out_path = tmp_path / "plan.dxf"
    write_dxf(
        [PlantingItem(geometry=Point(50, 50), planting_type="tree", species="Липа", score=0.8, rationale="r")],
        out_path,
        base_dxf=source,
    )

    after = ezdxf.readfile(str(out_path))
    after_entities = [(e.dxftype(), e.dxf.layer) for e in after.modelspace()]

    # Every source entity survives, on its original layer.
    for entity in source_entities:
        assert entity in after_entities
    assert source_layers <= {layer.dxf.name for layer in after.layers}
    # And the result is additive, on its own prefixed layer.
    result_layers = {e.dxf.layer for e in after.modelspace() if e.dxf.layer.startswith(f"{RESULT_LAYER_PREFIX}$")}
    assert result_layers == {result_layer_name("tree")}


def test_writing_into_a_layer_the_source_owns_is_refused(tmp_path):
    """Silently merging into an existing layer is the "исходные слои не
    перезаписываются" rule being broken in the way nobody notices.
    """
    doc = ezdxf.new(setup=True)
    doc.layers.add(name=result_layer_name("tree"))
    source = tmp_path / "collide.dxf"
    doc.saveas(str(source))

    items = [PlantingItem(geometry=Point(1, 1), planting_type="tree", species="Липа", score=0.5, rationale="r")]

    with pytest.raises(ResultLayerCollisionError):
        write_dxf(items, tmp_path / "out.dxf", base_dxf=source)


def test_justification_travels_with_the_entity_as_xdata(tmp_path):
    """The report alone satisfies the brief, but an expert checking in CAD
    should not have to open a JSON file to see which clause put a tree here.
    """
    from geo_engine.compliance import explain_items
    from geo_engine.model import Utility
    from geo_engine.norms import load_norms

    item = PlantingItem(geometry=Point(50, 50), planting_type="tree", species="Липа", score=0.8, rationale="r")
    utilities = [Utility(geometry=LineString([(0, 0), (100, 0)]), object_type="gas_pipe")]
    records = explain_items([item], utilities, [], load_norms())

    out_path = tmp_path / "annotated.dxf"
    write_dxf([item], out_path, records=records)

    doc = ezdxf.readfile(str(out_path))
    circle = next(e for e in doc.modelspace() if e.dxftype() == "CIRCLE")
    xdata = circle.get_xdata(RESULT_LAYER_PREFIX)
    text = "".join(value for code, value in xdata if code == 1000)

    assert "СП 42.13330.2016" in text
