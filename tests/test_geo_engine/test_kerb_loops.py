"""Замкнутый контур бортового камня — проезжая часть только при подтверждении.

dxf_reader._drop_non_carriageway_kerb_loops: сверка с посадочными чертежами
проектировщиков показала, что замкнутые контуры борта — это в основном
газонные и разделительные полосы, островки и кварталы (там и стоят его
деревья), а проезжая часть лежит между линиями борта и замыкается редко.
"""

from __future__ import annotations

from shapely.geometry import LineString, Point, Polygon, box

from geo_engine.io.dxf_reader import _drop_non_carriageway_kerb_loops
from geo_engine.model import Zone

KERB = {"layer": "БР 1000 300 150"}
FILL = {"layer": "ДВ_ПП_ДО_Тип2_Ремонт_покрытия_ПЧ_Местные"}


def _roads(zones: list[Zone]) -> list[Polygon]:
    return [z.geometry for z in zones if z.zone_type == "road" and z.geometry.geom_type == "Polygon"]


def test_an_empty_kerb_loop_without_fills_stays_a_road():
    loop = Zone(box(0, 0, 20, 10), "road", KERB)
    assert _roads(_drop_non_carriageway_kerb_loops([loop])) == [loop.geometry]


def test_a_kerb_loop_with_existing_greenery_inside_is_not_a_road():
    loop = Zone(box(0, 0, 20, 10), "road", KERB)
    tree = Zone(Point(5, 5), "existing_greenery", {})
    zones = _drop_non_carriageway_kerb_loops([loop, tree])
    assert _roads(zones) == []
    assert tree in zones


def test_a_kerb_loop_around_a_building_is_not_a_road():
    loop = Zone(box(0, 0, 40, 40), "road", KERB)
    house = Zone(box(10, 10, 20, 20), "building", {})
    assert _roads(_drop_non_carriageway_kerb_loops([loop, house])) == []


def test_a_narrow_kerb_strip_is_not_a_road():
    """Полоса уже полосы движения — разделительная или газонная."""
    strip = Zone(box(0, 0, 60, 2.5), "road", KERB)
    assert _roads(_drop_non_carriageway_kerb_loops([strip])) == []


def test_with_carriageway_fills_only_covered_kerb_loops_stay_roads():
    fill = Zone(box(0, 0, 100, 10), "road", FILL)
    covered = Zone(box(10, 0, 30, 10), "road", KERB)
    island = Zone(box(0, 20, 30, 30), "road", KERB)
    roads = _roads(_drop_non_carriageway_kerb_loops([fill, covered, island]))
    assert fill.geometry in roads
    assert covered.geometry in roads
    assert island.geometry not in roads


def test_kerb_lines_themselves_are_kept():
    """Отступ от края проезжей части по-прежнему считается от линии борта."""
    line = Zone(LineString([(0, 0), (20, 0)]), "road", KERB)
    loop = Zone(box(0, 0, 20, 10), "road", KERB)
    tree = Zone(Point(5, 5), "existing_greenery", {})
    zones = _drop_non_carriageway_kerb_loops([line, loop, tree])
    assert line in zones


def test_duplicate_kerb_loops_collapse_to_one():
    """Второй, бандловый проход замыкает те же линии ещё раз."""
    a = Zone(box(0, 0, 20, 10), "road", KERB)
    b = Zone(box(0, 0, 20, 10), "road", KERB)
    assert len(_roads(_drop_non_carriageway_kerb_loops([a, b]))) == 1


def test_non_kerb_road_polygons_are_untouched():
    fill = Zone(box(0, 0, 20, 10), "road", FILL)
    tree = Zone(Point(5, 5), "existing_greenery", {})
    assert _roads(_drop_non_carriageway_kerb_loops([fill, tree])) == [fill.geometry]


def test_a_reconstructed_loop_needs_confirmation_whatever_layer_it_inherited():
    """Восстановленный контур получает слой первой линии списка — это может
    быть и слой заливки «ПЧ». Он всё равно гипотеза: не подтверждает другие
    контуры и сам проверяется."""
    from geo_engine.io.dxf_reader import RECONSTRUCTED_LOOP_ATTR

    fake_fill = Zone(box(0, 0, 20, 10), "road", {**FILL, RECONSTRUCTED_LOOP_ATTR: True})
    tree = Zone(Point(5, 5), "existing_greenery", {})
    assert _roads(_drop_non_carriageway_kerb_loops([fake_fill, tree])) == []


def test_closed_kerb_lines_read_from_a_drawing_are_tagged_as_reconstructed(tmp_path):
    import ezdxf

    from geo_engine.io.dxf_reader import MOSGEOTREST_LAYER_MAP, RECONSTRUCTED_LOOP_ATTR, read_dxf

    doc = ezdxf.new(setup=True)
    doc.layers.add(name="Бортовой камень")
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (10, 0), (10, 10), (0, 10)], dxfattribs={"layer": "Бортовой камень"})
    msp.add_line((0, 10), (0, 0), dxfattribs={"layer": "Бортовой камень"})
    path = tmp_path / "loop.dxf"
    doc.saveas(str(path))

    _, zones = read_dxf(path, layer_map=MOSGEOTREST_LAYER_MAP, reconstruct_footprints=True)
    polygons = [z for z in zones if z.zone_type == "road" and z.geometry.geom_type == "Polygon"]
    assert len(polygons) == 1
    assert polygons[0].attrs[RECONSTRUCTED_LOOP_ATTR] is True
