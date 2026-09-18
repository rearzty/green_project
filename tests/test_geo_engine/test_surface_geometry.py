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
"""

import ezdxf
import pytest

from geo_engine.io.dxf_reader import MOSGEOTREST_LAYER_MAP, read_dxf

BUILDING_LAYER = "Здания"


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

    def test_other_zone_types_are_not_touched_by_reconstruction(self, tmp_path):
        """Only RECONSTRUCT_FOOTPRINT_ZONE_TYPES (building) goes through
        polygonize() -- "road" stays the kerb line it is by design (see
        MOSGEOTREST_LAYER_MAP's "Бортовой камень" comment), not something
        this flag should silently reshape."""
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
