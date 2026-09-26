"""Real surface geometry the reader used to drop or leave as a zero-area line.

Two independent findings from "1. Олимпийская деревня", both about geometry
that looked fine in the DXF but was never actually usable by the pipeline:

* HATCH entities (a bureau's separate "Заливки" fills file — real asphalt/
  lawn/tile surface polygons, not decoration) fell through `_entity_to_geometry`
  to `None` — not misclassified, not "unknown", just silently absent from both
  utilities and zones. See `_hatch_to_geometry`.
* The real "Здания" layer draws a building outline as an *open* polyline
  (0 of 409 LWPOLYLINE entities checked were `is_closed`), so most buildings
  read as LineString — and `buffers.buildable_area()`'s hard-obstacle
  subtraction is a silent no-op against a zero-area line. See
  `geometry_cleanup.reconstruct_closed_footprints` for the isolated unit
  tests on the reconstruction logic itself; the tests here exercise the
  `read_dxf(reconstruct_footprints=True)` wiring end to end.

A third, independent finding from "2. Песчаный переулок": standalone ARC
entities (curb radius segments at corners/junctions, drawn as their own
entity rather than a bulge inside a polyline -- confirmed live: 197 of them
on that street's real curb layers) had no branch in `_entity_to_geometry` at
all and fell straight to `None`, same silent-vanishing failure mode as the
HATCH gap above but for a different entity type.
"""

import ezdxf
import ezdxf.acis.api as acis_api
import pytest
from ezdxf.render import MeshBuilder

from geo_engine.io.dxf_reader import MOSGEOTREST_LAYER_MAP, _entity_to_geometry, _region_to_geometry, read_dxf

BUILDING_LAYER = "Здания"


def _region_entity(doc, faces, layer="0", dxftype="REGION"):
    """A REGION/3DSOLID entity carrying a *real* ACIS payload, built the same
    way `_region_to_geometry`'s own docstring measures against real ODA
    output: `body_from_mesh` + `export_dxf` round-trip through ezdxf's own
    ACIS writer, not a hand-typed byte string standing in for one. Each item
    in `faces` is one flat (z=0) polygon's vertex ring.
    """
    mesh = MeshBuilder()
    for face in faces:
        mesh.add_face([(x, y, 0) for x, y in face])
    body = acis_api.body_from_mesh(mesh)
    entity = doc.modelspace().new_entity(dxftype, dxfattribs={"layer": layer})
    acis_api.export_dxf(entity, [body])
    return entity


class TestRegionGeometry:
    """REGION/3DSOLID as real polygon geometry — see `_region_to_geometry`'s
    own docstring for the full story (LibreDWG loses this entirely, ODA
    preserves it, verified against real REGION-bearing xrefs on "2. Песчаный
    переулок"). These tests exercise the shapely reconstruction itself
    against synthetic-but-real ACIS payloads, not the real pilot data.
    """

    def test_a_flat_region_becomes_a_polygon_with_the_right_area(self):
        doc = ezdxf.new("R2018")
        entity = _region_entity(doc, [[(0, 0), (10, 0), (10, 10), (0, 10)]])

        geometry = _region_to_geometry(entity)

        assert geometry.geom_type == "Polygon"
        assert geometry.area == pytest.approx(100.0)

    def test_two_disjoint_faces_on_one_body_union_into_a_multipolygon(self):
        doc = ezdxf.new("R2018")
        entity = _region_entity(
            doc, [[(0, 0), (10, 0), (10, 10), (0, 10)], [(20, 0), (25, 0), (25, 5), (20, 5)]]
        )

        geometry = _region_to_geometry(entity)

        assert geometry.geom_type == "MultiPolygon"
        assert geometry.area == pytest.approx(100.0 + 25.0)

    def test_a_3dsolid_entity_is_read_the_same_way_as_region(self):
        doc = ezdxf.new("R2018")
        entity = _region_entity(doc, [[(0, 0), (10, 0), (10, 10), (0, 10)]], dxftype="3DSOLID")

        geometry = _entity_to_geometry(entity)

        assert geometry.geom_type == "Polygon"
        assert geometry.area == pytest.approx(100.0)

    def test_a_region_with_no_acis_payload_is_skipped_not_crashed(self):
        """Live case: LibreDWG converts a real bureau REGION to a 0-byte ACIS
        payload (verified directly, not assumed) — this must degrade the same
        way an unreadable HATCH boundary does (silently absent), not raise,
        since the file otherwise reads fine."""
        doc = ezdxf.new("R2018")
        entity = doc.modelspace().new_entity("REGION", dxfattribs={"layer": "0"})

        assert entity.acis_data == b""
        assert _region_to_geometry(entity) is None

    def test_a_region_on_a_mapped_layer_reaches_read_dxf_as_the_right_zone_type(self, tmp_path):
        """End-to-end, same shape as TestHatchGeometry above: a REGION on a
        real, already-classified layer name must come out the other end of
        `read_dxf` as that zone type with real polygon area — no new
        colour-based classification needed, the existing layer_map already
        does the job once the geometry branch exists at all."""
        doc = ezdxf.new("R2018")
        _region_entity(doc, [[(0, 0), (10, 0), (10, 10), (0, 10)]], layer="Газопровод")

        path = tmp_path / "region_gas.dxf"
        doc.saveas(str(path))

        utilities, _ = read_dxf(path, layer_map=MOSGEOTREST_LAYER_MAP)

        gas = [u for u in utilities if u.object_type == "gas_pipe"]
        assert len(gas) == 1
        assert gas[0].geometry.geom_type == "Polygon"
        assert gas[0].geometry.area == pytest.approx(100.0)


class TestAcisDataLookupIsIndexed:
    """Live perf bug, found on "4. Харьковская улица": every REGION/3DSOLID's
    `entity.sab` access -- what `_region_to_geometry()`'s `ezdxf.acis.api.
    load_dxf()` call triggers once per entity -- made ezdxf's own
    `AcDsDataSection.find_acis_record()` do a linear scan through the whole
    ACDSDATA section, so a document with N ACIS entities cost O(N²) to read,
    not O(N). Measured directly: 104s of a 106s file read, on a real 60.7 МБ
    file with 6503 REGION entities -- almost the entire per-file bottleneck
    of the whole bundle read. Patched with a handle -> record index built
    once per section and reused (`_indexed_find_acis_record`, applied at
    import time in dxf_reader.py, same place/style as this module's other
    ezdxf patches) -- 104s -> 3.9s on that same real file, ~27x, with
    byte-identical lookups spot-checked against the original linear scan
    across the start/middle/end of the list (0 mismatches over 150 checked).

    These tests exercise the patched function directly against a real
    multi-entity ACDSDATA section (built the same way TestRegionGeometry
    builds its ACIS payloads: `body_from_mesh()` + `export_dxf()`, not a
    hand-typed byte string), not a timing assertion -- a wall-clock threshold
    here would either be too loose to catch a real O(N²) regression or too
    tight to survive a slow CI runner. What actually matters and is safe to
    assert unconditionally: every entity's own data is still found, correctly,
    including after the ACDSDATA section grows.
    """

    def _region_with_acis(self, doc, tag: float):
        entity = doc.modelspace().new_entity("REGION", dxfattribs={"layer": "0"})
        mesh = MeshBuilder()
        mesh.add_face([(0, 0, 0), (tag, 0, 0), (tag, tag, 0), (0, tag, 0)])
        body = acis_api.body_from_mesh(mesh)
        acis_api.export_dxf(entity, [body])
        return entity

    def test_every_entitys_own_data_is_found_correctly(self):
        """Not just "a lookup succeeds" -- each of several entities in the
        same document must get back *its own* data, not a neighbour's
        (the failure mode an off-by-one or a stale index would produce)."""
        doc = ezdxf.new("R2018")
        entities = [self._region_with_acis(doc, tag=float(n)) for n in (3, 7, 11, 19)]

        for entity, expected_side in zip(entities, (3.0, 7.0, 11.0, 19.0)):
            geometry = _region_to_geometry(entity)
            assert geometry.area == pytest.approx(expected_side * expected_side)

    def test_lookup_still_works_after_the_acdsdata_section_grows(self):
        """The index invalidates on `len(entities)` change -- built once for
        the first two entities, then a third is added (growing the section)
        and must still resolve correctly, not miss because of a stale index
        built before it existed."""
        doc = ezdxf.new("R2018")
        first = self._region_with_acis(doc, tag=5.0)
        second = self._region_with_acis(doc, tag=8.0)

        assert _region_to_geometry(first).area == pytest.approx(25.0)
        assert _region_to_geometry(second).area == pytest.approx(64.0)

        third = self._region_with_acis(doc, tag=13.0)
        assert _region_to_geometry(third).area == pytest.approx(169.0)
        # The first two must still resolve correctly too, not just the new one.
        assert _region_to_geometry(first).area == pytest.approx(25.0)
        assert _region_to_geometry(second).area == pytest.approx(64.0)

    def test_a_handle_with_no_acis_record_is_not_found(self):
        """A REGION that legitimately carries no ACIS payload (the LibreDWG
        0-byte case `_region_to_geometry`'s own tests cover) must not be
        confused with a real record by the indexed lookup either."""
        doc = ezdxf.new("R2018")
        self._region_with_acis(doc, tag=4.0)
        empty = doc.modelspace().new_entity("REGION", dxfattribs={"layer": "0"})

        assert doc.acdsdata.find_acis_record(empty.dxf.handle) is None


class TestHatchGeometry:
    def test_a_simple_polyline_boundary_hatch_becomes_a_polygon(self, tmp_path):
        doc = ezdxf.new(setup=True)
        doc.layers.add(name=BUILDING_LAYER)
        msp = doc.modelspace()
        hatch = msp.add_hatch(dxfattribs={"layer": BUILDING_LAYER})
        hatch.paths.add_polyline_path([(0, 0), (10, 0), (10, 10), (0, 10)], is_closed=True)

        path = tmp_path / "hatch.dxf"
        doc.saveas(str(path))

        _, zones = read_dxf(path, layer_map=MOSGEOTREST_LAYER_MAP)

        buildings = [z for z in zones if z.zone_type == "building"]
        assert len(buildings) == 1
        assert buildings[0].geometry.geom_type == "Polygon"
        assert buildings[0].geometry.area == pytest.approx(100.0)

    def test_an_edge_path_hatch_with_a_curved_edge_still_yields_a_real_polygon(self, tmp_path):
        """The seam hatches in the real "Заливки" file are mostly straight
        polyline boundaries, but HATCH also allows an EdgePath of LINE/ARC/
        SPLINE edges — this must not silently vanish just because one edge
        isn't a straight line."""
        doc = ezdxf.new(setup=True)
        doc.layers.add(name=BUILDING_LAYER)
        msp = doc.modelspace()
        hatch = msp.add_hatch(dxfattribs={"layer": BUILDING_LAYER})
        edge_path = hatch.paths.add_edge_path()
        edge_path.add_line((0, 0), (10, 0))
        edge_path.add_line((10, 0), (10, 10))
        edge_path.add_arc(center=(5, 10), radius=5, start_angle=0, end_angle=180)
        edge_path.add_line((0, 10), (0, 0))

        path = tmp_path / "hatch_arc.dxf"
        doc.saveas(str(path))

        _, zones = read_dxf(path, layer_map=MOSGEOTREST_LAYER_MAP)

        buildings = [z for z in zones if z.zone_type == "building"]
        assert len(buildings) == 1
        assert buildings[0].geometry.geom_type == "Polygon"
        # Rectangle (100) plus the half-disc the arc bulges out into (~39.3)
        # -- generous tolerance since the arc is flattened to segments, not
        # exact.
        assert buildings[0].geometry.area == pytest.approx(139.3, abs=2.0)

    def test_a_hatch_with_an_inner_hole_loop_subtracts_the_hole(self, tmp_path):
        doc = ezdxf.new(setup=True)
        doc.layers.add(name=BUILDING_LAYER)
        msp = doc.modelspace()
        hatch = msp.add_hatch(dxfattribs={"layer": BUILDING_LAYER})
        hatch.paths.add_polyline_path([(0, 0), (20, 0), (20, 20), (0, 20)], is_closed=True)
        hatch.paths.add_polyline_path([(5, 5), (15, 5), (15, 15), (5, 15)], is_closed=True)

        path = tmp_path / "hatch_hole.dxf"
        doc.saveas(str(path))

        _, zones = read_dxf(path, layer_map=MOSGEOTREST_LAYER_MAP)

        buildings = [z for z in zones if z.zone_type == "building"]
        assert len(buildings) == 1
        # 20x20 outer minus 10x10 inner hole = 300, not 400.
        assert buildings[0].geometry.area == pytest.approx(300.0)


class TestArcGeometry:
    def test_a_standalone_arc_becomes_a_flattened_linestring(self):
        doc = ezdxf.new()
        msp = doc.modelspace()
        arc = msp.add_arc(center=(0, 0), radius=10, start_angle=0, end_angle=90, dxfattribs={"layer": "x"})

        geometry = _entity_to_geometry(arc)

        assert geometry is not None
        assert geometry.geom_type == "LineString"
        coords = list(geometry.coords)
        assert coords[0] == pytest.approx((10.0, 0.0))
        assert coords[-1] == pytest.approx((0.0, 10.0), abs=1e-9)
        # A quarter circle of radius 10 has true arc length pi/2*10 ~= 15.708;
        # flattened to a short chain of straight segments it must be close
        # but strictly no longer than the true arc (chords are shorter).
        assert geometry.length == pytest.approx(15.708, abs=0.2)

    def test_a_real_kerb_radius_arc_is_read_as_a_road_zone(self, tmp_path):
        """Live case, "2. Песчаный переулок": this bureau draws curb radius
        segments at corners as standalone ARC entities on layers named
        through the English word "kerb" (`!Project_road kerb - БР100.30.15
        внутренний`) -- neither the bare-code `\\bбр\\b.*\\d+.*\\d+` rule
        (fails: "БР100" has no word boundary right after "р") nor the
        `\\bборт\\w*` rule (no Russian "борт" word present at all) matched
        before this test, and the ARC itself had no geometry branch either.
        """
        doc = ezdxf.new()
        msp = doc.modelspace()
        msp.add_arc(
            center=(0, 0), radius=5, start_angle=0, end_angle=90,
            dxfattribs={"layer": "!Project_road kerb - БР100.30.15 внутренний"},
        )

        path = tmp_path / "kerb_arc.dxf"
        doc.saveas(str(path))

        _, zones = read_dxf(path, layer_map=MOSGEOTREST_LAYER_MAP)

        roads = [z for z in zones if z.zone_type == "road"]
        assert len(roads) == 1
        assert roads[0].geometry.geom_type == "LineString"


class TestReconstructFootprintsWiring:
    """`read_dxf(reconstruct_footprints=...)` — the flag itself, not the
    reconstruction algorithm (see test_geometry_cleanup.py for that)."""

    def _open_building_drawing(self, tmp_path):
        doc = ezdxf.new(setup=True)
        doc.layers.add(name=BUILDING_LAYER)
        msp = doc.modelspace()
        # The exact real-data shape (see geometry_cleanup.py's docstring):
        # a 3-sided LWPOLYLINE that does NOT close (is_closed defaults to
        # False, gap to the start point is 10 m -- far past the reader's own
        # 5 cm close-tolerance heuristic), plus a separate LINE entity on the
        # same layer supplying the missing 4th edge.
        msp.add_lwpolyline([(0, 0), (10, 0), (10, 10), (0, 10)], dxfattribs={"layer": BUILDING_LAYER})
        msp.add_line((0, 10), (0, 0), dxfattribs={"layer": BUILDING_LAYER})
        path = tmp_path / "open_building.dxf"
        doc.saveas(str(path))
        return path

    def test_default_off_leaves_the_open_outline_as_two_separate_lines(self, tmp_path):
        path = self._open_building_drawing(tmp_path)

        _, zones = read_dxf(path, layer_map=MOSGEOTREST_LAYER_MAP)

        buildings = [z for z in zones if z.zone_type == "building"]
        assert len(buildings) == 2
        assert all(b.geometry.geom_type == "LineString" for b in buildings)

    def test_enabled_reconstructs_the_closed_footprint(self, tmp_path):
        path = self._open_building_drawing(tmp_path)

        _, zones = read_dxf(path, layer_map=MOSGEOTREST_LAYER_MAP, reconstruct_footprints=True)

        buildings = [z for z in zones if z.zone_type == "building"]
        assert len(buildings) == 1
        assert buildings[0].geometry.geom_type == "Polygon"
        assert buildings[0].geometry.area == pytest.approx(100.0)

    def test_a_zone_type_outside_the_footprint_dict_is_not_touched_by_reconstruction(self, tmp_path):
        """Zone types outside RECONSTRUCT_FOOTPRINT_ZONE_TYPES don't go
        through polygonize() replacement at all. "road" specifically also
        gets a *separate*, additive pass (see TestAdditiveRoadPolygonWiring
        below) -- this fixture's single open kerb run has no partner to
        close against, so that pass adds nothing here and the line survives
        exactly as read, same as before that pass existed."""
        doc = ezdxf.new(setup=True)
        doc.layers.add(name="Бортовой камень")
        msp = doc.modelspace()
        msp.add_lwpolyline([(0, 0), (100, 0)], dxfattribs={"layer": "Бортовой камень"})
        path = tmp_path / "kerb.dxf"
        doc.saveas(str(path))

        _, zones = read_dxf(path, layer_map=MOSGEOTREST_LAYER_MAP, reconstruct_footprints=True)

        roads = [z for z in zones if z.zone_type == "road"]
        assert len(roads) == 1
        assert roads[0].geometry.geom_type == "LineString"

    def test_already_closed_buildings_are_unaffected(self, tmp_path):
        doc = ezdxf.new(setup=True)
        doc.layers.add(name=BUILDING_LAYER)
        msp = doc.modelspace()
        msp.add_lwpolyline([(0, 0), (10, 0), (10, 10), (0, 10)], close=True, dxfattribs={"layer": BUILDING_LAYER})
        path = tmp_path / "closed_building.dxf"
        doc.saveas(str(path))

        _, zones = read_dxf(path, layer_map=MOSGEOTREST_LAYER_MAP, reconstruct_footprints=True)

        buildings = [z for z in zones if z.zone_type == "building"]
        assert len(buildings) == 1
        assert buildings[0].geometry.geom_type == "Polygon"
        assert buildings[0].geometry.area == pytest.approx(100.0)

    def test_territory_is_reconstructed_with_its_own_wider_snap_grid(self, tmp_path):
        """`RECONSTRUCT_FOOTPRINT_ZONE_TYPES` covers "territory" too, with its
        own (snap_grid_m, dangle_buffer_m) -- see
        test_geometry_cleanup.py::TestReconstructClosedFootprints for the
        reconstruction algorithm itself (this test is only about the
        wiring). A gap of 23 cm is past DEFAULT_SNAP_GRID_M (5 cm, what
        "building" still uses) but within what territory's wider grid
        bridges -- the live case behind this, 12. Наташинский пр-д, is not
        reproduced vertex-for-vertex here. Tolerance widened (5 -> 15) when
        the grid itself widened further (0.3 -> 2.0 m, see
        RECONSTRUCT_FOOTPRINT_ZONE_TYPES's comment for the second live case
        that forced that) -- a coarser grid snaps this fixture's own
        vertices by up to ~1 m each, which measurably moves the exact area
        of a shape this size without meaning the mechanism is wrong.
        """
        doc = ezdxf.new(setup=True)
        doc.layers.add(name="!Граница работ")
        msp = doc.modelspace()
        msp.add_lwpolyline(
            [(0, 0), (100, 0), (100, 80), (50, 110), (0, 80), (0.23, 0)],
            close=False,
            dxfattribs={"layer": "!Граница работ"},
        )
        path = tmp_path / "wide_gap_territory.dxf"
        doc.saveas(str(path))

        without_flag = read_dxf(path, layer_map=MOSGEOTREST_LAYER_MAP)[1]
        with_flag = read_dxf(path, layer_map=MOSGEOTREST_LAYER_MAP, reconstruct_footprints=True)[1]

        assert without_flag[0].geometry.geom_type == "LineString"
        territory = [z for z in with_flag if z.zone_type == "territory"]
        assert len(territory) == 1
        assert territory[0].geometry.geom_type == "Polygon"
        assert territory[0].geometry.area == pytest.approx(9488.5, abs=15.0)

    def test_an_unclosable_territory_fragment_is_dropped_not_faked_into_an_area(self, tmp_path):
        """Live case, 7. Нижние Поля ул: the only boundary-layer candidate in
        the whole bundle is an isolated stub with no partner to close it
        against. Buffering that into a fake sliver "territory" would turn a
        street with no usable boundary in this input into one that silently
        looks fine -- territory's dangle_buffer_m=0.0 (unlike building's)
        means it is dropped instead, same as with the flag off.
        """
        doc = ezdxf.new(setup=True)
        doc.layers.add(name="!Граница работ")
        msp = doc.modelspace()
        msp.add_lwpolyline([(0, 0), (9.4, 0)], close=False, dxfattribs={"layer": "!Граница работ"})
        path = tmp_path / "unclosable_stub.dxf"
        doc.saveas(str(path))

        _, zones = read_dxf(path, layer_map=MOSGEOTREST_LAYER_MAP, reconstruct_footprints=True)

        assert zones == []

    def test_existing_greenery_boundary_lines_are_reconstructed_into_fillable_polygons(self, tmp_path):
        """Live case, 4. Харьковская улица: "Полоса деревьев"/"Леса и
        газоны" arrive as boundary LineStrings around a green patch (92% of
        the layer's objects on that street), not filled polygons -- the map
        rendered them as scattered thin lines instead of a filled area, and
        buildable_area() was silently not subtracting them at all (a
        LineString has zero area). Same mechanism as building, joined
        RECONSTRUCT_FOOTPRINT_ZONE_TYPES for the same reason."""
        doc = ezdxf.new(setup=True)
        doc.layers.add(name="Леса и газоны")
        msp = doc.modelspace()
        msp.add_lwpolyline([(0, 0), (20, 0), (20, 20), (0, 20)], dxfattribs={"layer": "Леса и газоны"})
        msp.add_line((0, 20), (0, 0), dxfattribs={"layer": "Леса и газоны"})
        path = tmp_path / "greenery_boundary.dxf"
        doc.saveas(str(path))

        _, zones = read_dxf(path, layer_map=MOSGEOTREST_LAYER_MAP, reconstruct_footprints=True)

        greenery = [z for z in zones if z.zone_type == "existing_greenery"]
        assert len(greenery) == 1
        assert greenery[0].geometry.geom_type == "Polygon"
        assert greenery[0].geometry.area == pytest.approx(400.0)

    def test_existing_greenery_tree_points_survive_reconstruction_untouched(self, tmp_path):
        """The same real layer also carries individual existing-tree points
        ("Отдельно стоящее дерево") alongside the boundary lines above --
        reconstruct_closed_footprints() has no branch for Point geometry and
        would silently drop it if handed the whole mixed list, which for a
        real existing tree is a correctness regression (a new candidate
        could then legally land right on top of it), not just a cosmetic
        loss. This is the regression test for the point/linelike split in
        the reconstruction loop, not just the polygon case above."""
        doc = ezdxf.new(setup=True)
        doc.layers.add(name="Отдельно стоящее дерево")
        doc.layers.add(name="Леса и газоны")
        msp = doc.modelspace()
        msp.add_point((5, 5), dxfattribs={"layer": "Отдельно стоящее дерево"})
        msp.add_lwpolyline([(50, 50), (70, 50), (70, 70), (50, 70)], close=True, dxfattribs={"layer": "Леса и газоны"})
        path = tmp_path / "greenery_mixed.dxf"
        doc.saveas(str(path))

        _, zones = read_dxf(path, layer_map=MOSGEOTREST_LAYER_MAP, reconstruct_footprints=True)

        greenery = [z for z in zones if z.zone_type == "existing_greenery"]
        points = [z for z in greenery if z.geometry.geom_type == "Point"]
        polygons = [z for z in greenery if z.geometry.geom_type == "Polygon"]
        assert len(points) == 1
        assert (points[0].geometry.x, points[0].geometry.y) == (5, 5)
        assert len(polygons) == 1
        assert polygons[0].geometry.area == pytest.approx(400.0)


class TestAdditiveRoadPolygonWiring:
    """`read_dxf(reconstruct_footprints=True)`'s separate, additive step for
    "road"/"sidewalk" (see geometry_cleanup.reconstruct_closed_road_polygons'
    docstring for why it can't reuse the building-style replace-in-place
    reconstruction above): a closed curb loop adds a NEW Polygon zone
    alongside the original line zones, which stay exactly as read.
    """

    def test_a_closed_kerb_loop_adds_a_polygon_without_removing_the_lines(self, tmp_path):
        doc = ezdxf.new(setup=True)
        doc.layers.add(name="Бортовой камень")
        msp = doc.modelspace()
        # Open 3-sided run + a separate closing LINE -- so the gap is metres,
        # not the reader's own 5 cm close-tolerance, and `_entity_to_geometry`
        # reads both as LineString; only polygonize() inside the additive
        # step should close this into a ring.
        msp.add_lwpolyline([(0, 0), (10, 0), (10, 10), (0, 10)], dxfattribs={"layer": "Бортовой камень"})
        msp.add_line((0, 10), (0, 0), dxfattribs={"layer": "Бортовой камень"})
        path = tmp_path / "closed_kerb.dxf"
        doc.saveas(str(path))

        _, zones = read_dxf(path, layer_map=MOSGEOTREST_LAYER_MAP, reconstruct_footprints=True)

        roads = [z for z in zones if z.zone_type == "road"]
        lines = [r for r in roads if r.geometry.geom_type == "LineString"]
        polygons = [r for r in roads if r.geometry.geom_type == "Polygon"]
        # The two original lines survive untouched, alongside the new polygon.
        assert len(lines) == 2
        assert len(polygons) == 1
        assert polygons[0].geometry.area == pytest.approx(100.0)

    def test_default_off_never_runs_the_additive_step_either(self, tmp_path):
        doc = ezdxf.new(setup=True)
        doc.layers.add(name="Бортовой камень")
        msp = doc.modelspace()
        msp.add_lwpolyline(
            [(0, 0), (10, 0), (10, 10), (0, 10)],
            dxfattribs={"layer": "Бортовой камень"},
        )
        msp.add_line((0, 10), (0, 0), dxfattribs={"layer": "Бортовой камень"})
        path = tmp_path / "closed_kerb_default_off.dxf"
        doc.saveas(str(path))

        _, zones = read_dxf(path, layer_map=MOSGEOTREST_LAYER_MAP)

        roads = [z for z in zones if z.zone_type == "road"]
        assert len(roads) == 2
        assert all(r.geometry.geom_type == "LineString" for r in roads)

    def test_buildable_area_now_actually_excludes_the_closed_road_polygon(self, tmp_path):
        """The point of the whole feature: buffers.HARD_OBSTACLE_ZONE_TYPES
        has always listed "road", but it was a no-op because road never had
        Polygon geometry for that filter to catch. This is the first test
        anywhere in the suite that exercises that no-op turning real."""
        from shapely.geometry import Polygon as ShapelyPolygon

        from geo_engine.buffers import buildable_area

        doc = ezdxf.new(setup=True)
        doc.layers.add(name="Бортовой камень")
        msp = doc.modelspace()
        # A closed kerb loop sitting entirely inside a larger territory --
        # e.g. a traffic island or a courtyard drive ring. Split open
        # run + closing LINE, same as the wiring test above, so this
        # specifically exercises the additive polygonize() step rather than
        # a Polygon that _entity_to_geometry already closed on its own.
        msp.add_lwpolyline([(20, 20), (40, 20), (40, 40), (20, 40)], dxfattribs={"layer": "Бортовой камень"})
        msp.add_line((20, 40), (20, 20), dxfattribs={"layer": "Бортовой камень"})
        path = tmp_path / "island_kerb.dxf"
        doc.saveas(str(path))

        _, zones = read_dxf(path, layer_map=MOSGEOTREST_LAYER_MAP, reconstruct_footprints=True)
        territory_geom = ShapelyPolygon([(0, 0), (0, 100), (100, 100), (100, 0)])

        area = buildable_area(territory_geom, None, zones)

        # The 20x20 traffic island (400 m^2) is now a real hard obstacle --
        # before this feature it would have been fully plantable.
        island = ShapelyPolygon([(20, 20), (40, 20), (40, 40), (20, 40)])
        assert area.intersection(island).area == pytest.approx(0.0, abs=1.0)
        # Everything outside the island is untouched.
        assert area.area == pytest.approx(100 * 100 - 20 * 20, abs=1.0)
