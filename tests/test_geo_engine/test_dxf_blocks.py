"""Reading geometry out of block references and xref-namespaced layers.

Both behaviours are what separates the synthetic DXF fixtures used elsewhere in
this suite from the real drawings in the pilot dataset. On one real geobase
sheet, modelspace holds 50 polylines while the same file expands to 6465 linear
entities once blocks are followed — so a reader that iterates modelspace flat
silently imports under 1% of the utilities and still reports success.
"""

import ezdxf
import pytest
from shapely.geometry import LineString, Point

from geo_engine.io.dxf_reader import (
    MOSGEOTREST_LAYER_MAP,
    SYMBOL_LAYERS,
    normalize_layer,
    read_dxf,
)

GAS_LAYER = "Газопровод"
TREE_LAYER = "Отдельно стоящее дерево"


def _drawing_with_nested_block(tmp_path, layer=GAS_LAYER, insert_layer=None):
    """A pipe run buried two block levels deep, the way MicroStation exports it.

    outer block -> INSERT of inner block -> the actual polyline.
    """
    doc = ezdxf.new(setup=True)
    doc.layers.add(name=layer)

    inner = doc.blocks.new(name="msdElementTypeLineString")
    inner.add_lwpolyline([(0, 0), (10, 0), (20, 5)], dxfattribs={"layer": layer})

    outer = doc.blocks.new(name="msdElementTypeMultiLine")
    outer.add_blockref("msdElementTypeLineString", (0, 0), dxfattribs={"layer": layer})

    msp = doc.modelspace()
    msp.add_blockref("msdElementTypeMultiLine", (0, 0), dxfattribs={"layer": insert_layer or layer})

    path = tmp_path / "nested.dxf"
    doc.saveas(str(path))
    return path


def test_geometry_inside_nested_blocks_is_imported(tmp_path):
    path = _drawing_with_nested_block(tmp_path)

    utilities, _ = read_dxf(path, layer_map=MOSGEOTREST_LAYER_MAP)

    assert [u.object_type for u in utilities] == ["gas_pipe"]
    assert isinstance(utilities[0].geometry, LineString)


def test_not_exploding_blocks_loses_that_geometry(tmp_path):
    """The old behaviour, pinned so the difference stays visible: without
    descending into blocks the pipe is invisible and the import looks clean.
    """
    path = _drawing_with_nested_block(tmp_path)

    utilities, _ = read_dxf(path, layer_map=MOSGEOTREST_LAYER_MAP, explode_blocks=False)

    assert utilities == []


def test_xref_prefixed_layer_names_resolve_to_the_same_type(tmp_path):
    """Attached xrefs namespace their layers as `xref196297|Газопровод`, so the
    same logical layer shows up once per referencing sheet.
    """
    path = _drawing_with_nested_block(tmp_path, insert_layer=f"xref196297|{GAS_LAYER}")

    utilities, _ = read_dxf(path, layer_map=MOSGEOTREST_LAYER_MAP)

    assert [u.object_type for u in utilities] == ["gas_pipe"]


def test_normalize_layer_only_strips_the_xref_prefix():
    assert normalize_layer("xref196297|!Граница работ") == "!Граница работ"
    assert normalize_layer(GAS_LAYER) == GAS_LAYER
    assert normalize_layer("!!!_1. ГРАНИЦА РАБОТ") == "!!!_1. ГРАНИЦА РАБОТ"


def test_symbol_layer_insert_becomes_one_point_not_its_drawn_parts(tmp_path):
    """An existing tree is a symbol block: its insertion point is the tree.
    Exploding it would yield the circles and ticks it is drawn from, turning one
    tree into several unrelated features.
    """
    doc = ezdxf.new(setup=True)
    doc.layers.add(name=TREE_LAYER)
    symbol = doc.blocks.new(name="DEREVO")
    symbol.add_circle((0, 0), radius=1.5, dxfattribs={"layer": TREE_LAYER})
    symbol.add_line((-2, 0), (2, 0), dxfattribs={"layer": TREE_LAYER})
    symbol.add_line((0, -2), (0, 2), dxfattribs={"layer": TREE_LAYER})
    doc.modelspace().add_blockref("DEREVO", (700, 14500), dxfattribs={"layer": TREE_LAYER})

    path = tmp_path / "tree.dxf"
    doc.saveas(str(path))

    _, zones = read_dxf(path, layer_map=MOSGEOTREST_LAYER_MAP)
    greenery = [z for z in zones if z.zone_type == "existing_greenery"]

    assert len(greenery) == 1
    assert greenery[0].geometry == Point(700, 14500)


def test_insert_on_an_unmapped_layer_is_not_exploded(tmp_path):
    """Found live on the pilot dataset: a telecom (MGTS) manhole/well block on
    a layer nobody mapped exploded into hundreds of decorative
    LINE/SPLINE/ELLIPSE/HATCH primitives that all resolve to
    object_type="unknown" anyway -- pure parse-time cost, no effect on the
    result. One INSERT with many primitives, on an unmapped layer, must
    collapse to exactly one unknown zone, not one per primitive.

    Uses a synthetic layer name, not the real "МГТС_ существ. ККС" one this
    was found on -- layer_rules.classify_layer() (added by a parallel PR,
    merged after this fix) actually recognizes that real name as a cable run
    via its "существ"/"ККС" pattern, so it is no longer a genuinely unmapped
    layer and correctly explodes now (see iter_entities's use_layer_rules
    param). This test needs a layer neither the literal map nor the rules
    ever classify, to keep testing the short-circuit itself.
    """
    unmapped_layer = "XYZ_random_layer_99"
    doc = ezdxf.new(setup=True)
    doc.layers.add(name=unmapped_layer)
    symbol = doc.blocks.new(name="*U5")
    for i in range(20):
        symbol.add_line((i, 0), (i, 1), dxfattribs={"layer": unmapped_layer})
    doc.modelspace().add_blockref("*U5", (10, 10), dxfattribs={"layer": unmapped_layer})

    path = tmp_path / "well.dxf"
    doc.saveas(str(path))

    utilities, zones = read_dxf(path, layer_map=MOSGEOTREST_LAYER_MAP)

    assert utilities == []
    assert len(zones) == 1
    assert zones[0].zone_type == "unknown"
    assert zones[0].geometry == Point(10, 10)


def test_insert_on_a_mapped_layer_still_explodes_fully(tmp_path):
    """The optimisation above must not regress the exact bug it sits next to
    (test_geometry_inside_nested_blocks_is_imported) -- a block on a real,
    mapped utility layer still descends into its actual line geometry."""
    path = _drawing_with_nested_block(tmp_path)

    utilities, _ = read_dxf(path, layer_map=MOSGEOTREST_LAYER_MAP)

    assert [u.object_type for u in utilities] == ["gas_pipe"]
    assert isinstance(utilities[0].geometry, LineString)


def test_a_block_on_an_unmapped_layer_still_explodes_if_its_content_is_classifiable(tmp_path):
    """Live data-loss bug, "20. Макеева С. ул": a real building outline
    (layer "Здания", a mapped layer) lived inside a block referenced on
    layer "0" -- ordinary, unremarkable AutoCAD practice (placing a block
    reference on layer "0" so its content keeps its own layers/colours), not
    a sign the content is decorative. The short-circuit above only ever
    checked the INSERT's own layer, never what is actually inside the block,
    and threw the whole building away as one unclassified insertion point.
    `building`/`existing_greenery`/`power_line_corridor` all went from 0 (not
    under-counted -- entirely absent) to real numbers once fixed. Uses a
    synthetic building block on layer "0" for the same reason the unmapped-
    layer test above uses a synthetic layer: pins the mechanism, not the one
    real name it was found on.
    """
    doc = ezdxf.new(setup=True)
    doc.layers.add(name="Здания")
    building_block = doc.blocks.new(name="BuildingBlock")
    building_block.add_lwpolyline([(0, 0), (10, 0), (10, 10), (0, 10)], close=True, dxfattribs={"layer": "Здания"})
    doc.modelspace().add_blockref("BuildingBlock", (0, 0), dxfattribs={"layer": "0"})

    path = tmp_path / "building_on_layer_zero.dxf"
    doc.saveas(str(path))

    _, zones = read_dxf(path, layer_map=MOSGEOTREST_LAYER_MAP)
    buildings = [z for z in zones if z.zone_type == "building"]

    assert len(buildings) == 1
    assert buildings[0].geometry.geom_type == "Polygon"
    assert buildings[0].geometry.area == pytest.approx(100.0)


def test_the_block_content_check_is_still_memoized_per_block_not_per_instance(tmp_path):
    """The fix above must not give up the original optimisation's real win:
    a block with genuinely unclassifiable content, referenced many times
    (the live MGTS-well case this short-circuit was built for), is still
    inspected once -- not re-exploded, and not re-scanned, per instance.
    """
    unmapped_layer = "XYZ_random_layer_99"
    doc = ezdxf.new(setup=True)
    doc.layers.add(name=unmapped_layer)
    symbol = doc.blocks.new(name="*U9")
    for i in range(20):
        symbol.add_line((i, 0), (i, 1), dxfattribs={"layer": unmapped_layer})
    msp = doc.modelspace()
    for i in range(5):
        msp.add_blockref("*U9", (i * 10, 10), dxfattribs={"layer": unmapped_layer})

    path = tmp_path / "many_wells.dxf"
    doc.saveas(str(path))

    utilities, zones = read_dxf(path, layer_map=MOSGEOTREST_LAYER_MAP)

    assert utilities == []
    assert len(zones) == 5
    assert all(z.zone_type == "unknown" for z in zones)


def test_classifiable_content_nested_two_blocks_deep_is_still_found(tmp_path):
    """The container layer test recurses through nested INSERTs (the same
    MicroStation two-level pattern `_drawing_with_nested_block` above
    exercises for the "explode at all" question) -- it must not stop at the
    first level when deciding whether a block is worth exploding either."""
    doc = ezdxf.new(setup=True)
    doc.layers.add(name="Здания")
    inner = doc.blocks.new(name="InnerBuilding")
    inner.add_lwpolyline([(0, 0), (10, 0), (10, 10), (0, 10)], close=True, dxfattribs={"layer": "Здания"})
    outer = doc.blocks.new(name="OuterWrapper")
    outer.add_blockref("InnerBuilding", (0, 0), dxfattribs={"layer": "0"})
    doc.modelspace().add_blockref("OuterWrapper", (0, 0), dxfattribs={"layer": "0"})

    path = tmp_path / "nested_building_on_layer_zero.dxf"
    doc.saveas(str(path))

    _, zones = read_dxf(path, layer_map=MOSGEOTREST_LAYER_MAP)
    buildings = [z for z in zones if z.zone_type == "building"]

    assert len(buildings) == 1
    assert buildings[0].geometry.geom_type == "Polygon"


def test_symbol_layers_are_a_subset_of_what_the_map_knows_about():
    """Not every symbol layer needs a mapping, but a symbol layer that *is*
    mapped must stay mapped — otherwise its objects quietly become "unknown".
    """
    assert TREE_LAYER in SYMBOL_LAYERS
    assert MOSGEOTREST_LAYER_MAP[TREE_LAYER] == ("zone", "existing_greenery")


@pytest.mark.parametrize(
    "layer, expected",
    [
        ("Газопровод", "gas_pipe"),
        ("Теплосеть", "heat_network"),
        ("Водопровод", "water_pipe"),
        ("Канализация самотёчная", "sewer"),
        ("Водосток", "sewer"),
        ("Кабель электрический", "cable_line"),
        ("Кабель связи", "cable_line"),
        ("Кабели", "cable_line"),
        ("ЛЭП", "power_line_corridor"),
    ],
)
def test_every_mapped_utility_layer_has_a_setback_in_the_norms(layer, expected):
    """A utility whose object_type has no setbacks_m entry gets clearance 0 and
    silently stops constraining anything — the failure mode is an invisible one.
    """
    from geo_engine.norms import load_norms

    kind, object_type = MOSGEOTREST_LAYER_MAP[layer]
    assert kind == "utility"
    assert object_type == expected
    assert load_norms().setback_for(object_type, "tree") > 0


BOUNDARY_LAYER = "!Граница работ"


def test_polyline_closed_by_geometry_but_not_by_flag_reads_as_an_area(tmp_path):
    """The pilot work-area outline is drawn this way: 331 vertices,
    is_closed=False, first and last points 11 mm apart. As a LineString it is
    useless — territory_polygon() needs an area.
    """
    doc = ezdxf.new(setup=True)
    doc.layers.add(name=BOUNDARY_LAYER)
    # Gap of 11 mm, matching the real outline. Deliberately not laid back along
    # the first edge: a ring whose last point sits *on* an existing segment is
    # self-touching and shapely calls it invalid, which is a different case —
    # the reader leaves those as lines rather than guessing at a repair (see
    # test_a_self_touching_near_closed_ring_is_left_as_a_line below, and
    # reconstruct_closed_footprints() for the real fix path for that class).
    ring = [(0, 0), (100, 0), (100, 80), (50, 110), (0, 80), (0.008, 0.008)]
    doc.modelspace().add_lwpolyline(ring, close=False, dxfattribs={"layer": BOUNDARY_LAYER})
    path = tmp_path / "outline.dxf"
    doc.saveas(str(path))

    _, zones = read_dxf(path, layer_map=MOSGEOTREST_LAYER_MAP)
    territory = [z for z in zones if z.zone_type == "territory"]

    assert len(territory) == 1
    assert territory[0].geometry.geom_type == "Polygon"


def test_a_genuinely_open_polyline_stays_a_line(tmp_path):
    """The tolerance must not turn every pipe run that wanders back near its
    own start into a polygon. 5 m apart is a real gap, not a drafting slip.
    """
    doc = ezdxf.new(setup=True)
    doc.layers.add(name=GAS_LAYER)
    doc.modelspace().add_lwpolyline(
        [(0, 0), (100, 0), (100, 80), (0, 80), (0, 5)], close=False, dxfattribs={"layer": GAS_LAYER}
    )
    path = tmp_path / "open.dxf"
    doc.saveas(str(path))

    utilities, _ = read_dxf(path, layer_map=MOSGEOTREST_LAYER_MAP)

    assert utilities[0].geometry.geom_type == "LineString"


def test_bundle_paths_find_the_xrefs_next_to_the_drawing(tmp_path):
    """A project drawing is not self-contained — its site outline and geobase
    live in Xrefs/ beside it, and ezdxf does not resolve those itself.
    """
    from geo_engine.io.dxf_reader import dxf_bundle_paths

    main = tmp_path / "plan.dxf"
    main.write_bytes(b"")
    xrefs = tmp_path / "Xrefs"
    xrefs.mkdir()
    (xrefs / "geobase.dxf").write_bytes(b"")
    (xrefs / "outline.dxf").write_bytes(b"")
    (xrefs / "notes.txt").write_bytes(b"")

    paths = dxf_bundle_paths(main)

    assert paths[0] == main
    assert [p.name for p in paths[1:]] == ["geobase.dxf", "outline.dxf"]


def test_bundle_read_merges_the_outline_from_a_sibling_file(tmp_path):
    """The case that matters: utilities in one file, the site outline in
    another, and only the two together can produce a plan.
    """
    from geo_engine.io.dxf_reader import dxf_bundle_paths, read_dxf_bundle

    main = ezdxf.new(setup=True)
    main.layers.add(name=GAS_LAYER)
    main.modelspace().add_lwpolyline([(10, 10), (90, 10)], dxfattribs={"layer": GAS_LAYER})
    main_path = tmp_path / "plan.dxf"
    main.saveas(str(main_path))

    xrefs = tmp_path / "Xrefs"
    xrefs.mkdir()
    outline = ezdxf.new(setup=True)
    outline.layers.add(name=BOUNDARY_LAYER)
    outline.modelspace().add_lwpolyline(
        [(0, 0), (100, 0), (100, 80), (0, 80)], close=True, dxfattribs={"layer": BOUNDARY_LAYER}
    )
    outline.saveas(str(xrefs / "outline.dxf"))

    utilities, zones = read_dxf_bundle(dxf_bundle_paths(main_path), layer_map=MOSGEOTREST_LAYER_MAP)

    assert [u.object_type for u in utilities] == ["gas_pipe"]
    assert [z.zone_type for z in zones] == ["territory"]


def test_a_self_touching_near_closed_ring_is_left_as_a_line(tmp_path):
    """Closing is attempted, not forced: if the resulting ring is invalid the
    reader hands back the line rather than a silently repaired polygon whose
    shape nobody checked.
    """
    doc = ezdxf.new(setup=True)
    doc.layers.add(name=BOUNDARY_LAYER)
    # The last vertex lands on the first edge, so the ring touches itself.
    doc.modelspace().add_lwpolyline(
        [(0, 0), (100, 0), (100, 80), (0, 80), (0.011, 0)], close=False, dxfattribs={"layer": BOUNDARY_LAYER}
    )
    path = tmp_path / "selftouch.dxf"
    doc.saveas(str(path))

    _, zones = read_dxf(path, layer_map=MOSGEOTREST_LAYER_MAP)

    assert zones[0].geometry.geom_type == "LineString"


def test_an_explicitly_closed_self_intersecting_polyline_is_repaired_not_left_invalid(tmp_path):
    """Different from the test above on purpose: there the closure is
    inferred (ends merely land close together), so falling back to a line
    when the ring turns out invalid respects the source data -- nothing said
    this should have been an area. Here the DXF entity is explicitly flagged
    closed (`close=True`), so there is no such "maybe it wasn't meant to
    close" escape hatch -- the source data itself says it is a closed ring,
    and the only question is whether the *shape* is valid. Live crash, "13.
    Харьковский проезд": 7 of ~2600 sidewalk polygons on that street are
    exactly this -- an explicitly-closed LWPOLYLINE whose vertex sequence
    self-intersects (a real drafting slip or DWG->DXF conversion artifact,
    not inferred data) -- and the unrepaired invalid Polygon later crashed
    `buffers.buildable_area()`'s `unary_union(hard_obstacles)` with a GEOS
    `side location conflict` several call frames away from which polygon
    actually caused it.
    """
    doc = ezdxf.new(setup=True)
    doc.layers.add(name=BOUNDARY_LAYER)
    # A bowtie: crosses itself once, same shape class as the real finding.
    doc.modelspace().add_lwpolyline(
        [(0, 0), (100, 100), (100, 0), (0, 100)], close=True, dxfattribs={"layer": BOUNDARY_LAYER}
    )
    path = tmp_path / "bowtie.dxf"
    doc.saveas(str(path))

    _, zones = read_dxf(path, layer_map=MOSGEOTREST_LAYER_MAP)

    assert zones[0].geometry.geom_type in ("Polygon", "MultiPolygon")
    assert zones[0].geometry.is_valid
    assert zones[0].geometry.area > 0
