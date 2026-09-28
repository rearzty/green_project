"""layer_raster.py: rendering a project's loaded layers as per-group PNGs
instead of GeoJSON features. Exercised against transient (un-persisted) ORM
objects, same style as test_compliance_service.py -- covers layer_group_key
and render_layer_raster, the two pure functions. get_layer_raster itself
(the async caching wrapper around a real DB query) isn't covered here, same
as this repo's existing convention of not exercising DB writes/queries
through pytest (see test_project_service.py's own docstring) -- verified
live instead (see docs/worklog.md for the measured before/after).
"""

from __future__ import annotations

from PIL import Image
from shapely.geometry import LineString, Point, Polygon

import backend.app.services.layer_raster as layer_raster_module
from backend.app.db.models import Layer
from backend.app.services.geo_io import shape_to_db
from backend.app.services.layer_raster import layer_group_key, render_layer_raster

TERRITORY = Polygon([(0, 0), (0, 100), (100, 100), (100, 0)])
GAS_PIPE = LineString([(0, 50), (100, 50)])
BUILDING = Polygon([(10, 10), (10, 20), (20, 20), (20, 10)])


def _layer(**kwargs) -> Layer:
    defaults = {"id": "layer", "project_id": "project-1", "attrs": {}}
    return Layer(**{**defaults, **kwargs})


class TestLayerGroupKey:
    def test_utilities_share_one_group_regardless_of_medium(self):
        gas = _layer(kind="utility", object_type="gas_pipe", geometry=shape_to_db(GAS_PIPE))
        water = _layer(kind="utility", object_type="water_pipe", geometry=shape_to_db(GAS_PIPE))
        assert layer_group_key(gas) == layer_group_key(water) == "utility"

    def test_zoning_splits_by_category(self):
        residential = _layer(kind="zone", object_type="zoning", geometry=shape_to_db(TERRITORY), attrs={"zoning_category": "residential"})
        industrial = _layer(kind="zone", object_type="zoning", geometry=shape_to_db(TERRITORY), attrs={"zoning_category": "industrial"})
        assert layer_group_key(residential) == "zoning:residential"
        assert layer_group_key(industrial) == "zoning:industrial"

    def test_everything_else_groups_by_its_own_object_type(self):
        building = _layer(kind="zone", object_type="building", geometry=shape_to_db(BUILDING))
        assert layer_group_key(building) == "building"


class TestRenderLayerRaster:
    def test_a_polygon_with_a_hole_renders_the_hole_transparent(self):
        """Живая находка, «1. Олимпийская деревня»: territory_polygon()
        теперь честно возвращает Polygon с настоящими дырками (исключённые
        из работы дворы, geo_engine/territory.py::_combine_with_holes), а
        не всегда сплошной блин без внутренних колец. На RGBA-холсте
        fill=(0,0,0,0) реально очищает пиксели до прозрачности, не просто
        рисует «ничего» — иначе дырка осталась бы залита тем же синим.

        Пиксельные координаты считаются той же формулой, что использует сам
        модуль (territory bounds +- _CONTEXT_MARGIN_M, затем масштаб до
        _MAX_CANVAS_PX), а не угадываются -- единственный "single" слой
        здесь и есть territory_geom, так что его bounds точно совпадают с
        bounds холста минус отступ.
        """
        outer = Polygon([(0, 0), (0, 100), (100, 100), (100, 0)], holes=[[(30, 30), (30, 70), (70, 70), (70, 30)]])
        layers = [_layer(id="territory", kind="zone", object_type="territory", geometry=shape_to_db(outer))]

        result = render_layer_raster(layers, source_crs=None)

        territory_group = next(g for g in result.groups if g.key == "territory")
        image = Image.open(__import__("io").BytesIO(territory_group.png))

        margin = layer_raster_module._CONTEXT_MARGIN_M
        minx, miny, maxx, maxy = -margin, -margin, 100 + margin, 100 + margin
        scale = layer_raster_module._MAX_CANVAS_PX / max(maxx - minx, maxy - miny)

        def to_px(x, y):
            return round((x - minx) * scale), round((maxy - y) * scale)

        hole_center_px = to_px(50, 50)
        inside_outer_px = to_px(5, 5)
        assert image.getpixel(hole_center_px)[3] == 0
        assert image.getpixel(inside_outer_px)[3] > 0

    def test_empty_layers_yield_no_bounds_and_no_groups(self):
        result = render_layer_raster([], source_crs=None)
        assert result.bounds is None
        assert result.groups == []

    def test_groups_are_separate_valid_pngs_sized_to_their_own_content(self):
        layers = [
            _layer(id="territory", kind="zone", object_type="territory", geometry=shape_to_db(TERRITORY)),
            _layer(id="gas", kind="utility", object_type="gas_pipe", geometry=shape_to_db(GAS_PIPE)),
            _layer(id="building", kind="zone", object_type="building", geometry=shape_to_db(BUILDING)),
        ]
        result = render_layer_raster(layers, source_crs=None)

        keys = {g.key for g in result.groups}
        # existing_greenery/existing_lawn always get a row (see
        # render_layer_raster's own comment) even though this fixture has
        # neither -- that's the point of the always-shown pair, checked
        # explicitly below.
        assert keys == {"territory", "utility", "building", "existing_greenery", "existing_lawn"}
        real_groups = {"territory", "utility", "building"}
        for group in result.groups:
            assert group.count == (1 if group.key in real_groups else 0)
            image = Image.open(__import__("io").BytesIO(group.png))
            assert image.format == "PNG"
            assert image.mode == "RGBA"

    def test_all_groups_share_the_same_pixel_dimensions(self):
        """<ImageOverlay> stretches every group's PNG over the identical
        `bounds` rectangle -- if two groups' canvases had different pixel
        aspect ratios they'd overlay misaligned."""
        layers = [
            _layer(id="territory", kind="zone", object_type="territory", geometry=shape_to_db(TERRITORY)),
            _layer(id="gas", kind="utility", object_type="gas_pipe", geometry=shape_to_db(GAS_PIPE)),
        ]
        result = render_layer_raster(layers, source_crs=None)

        sizes = set()
        for group in result.groups:
            image = Image.open(__import__("io").BytesIO(group.png))
            sizes.add(image.size)
        assert len(sizes) == 1

    def test_zoning_extent_does_not_balloon_the_canvas_bounds(self):
        """A zoning polygon can span a whole neighbourhood -- with a
        resolvable territory to anchor on, the canvas frames that (plus its
        fixed context margin, see _CONTEXT_MARGIN_M), not the zoning's much
        bigger footprint."""
        huge_zoning = Polygon([(-10_000, -10_000), (-10_000, 10_000), (10_000, 10_000), (10_000, -10_000)])
        layers = [
            _layer(id="territory", kind="zone", object_type="territory", geometry=shape_to_db(TERRITORY)),
            _layer(id="zoning", kind="zone", object_type="zoning", geometry=shape_to_db(huge_zoning), attrs={"zoning_category": "residential"}),
        ]
        result = render_layer_raster(layers, source_crs=None)

        (south, west), (north, east) = result.bounds
        assert (west, south) == (-layer_raster_module._CONTEXT_MARGIN_M, -layer_raster_module._CONTEXT_MARGIN_M)
        assert (east, north) == (100 + layer_raster_module._CONTEXT_MARGIN_M, 100 + layer_raster_module._CONTEXT_MARGIN_M)

    def test_territory_group_shows_the_same_clustered_boundary_generation_uses(self):
        """Live case, 4. Харьковская улица: the real "Граница работ" layer
        also carries an isolated ~54,000 m2 fragment with zero utilities or
        other zones anywhere near it -- territory_polygon() (what
        buildable_area()/candidates actually work from) drops it, and the
        map's "territory" group must show the same thing, not the raw
        zone_type="territory" list with that discarded piece still in it.
        """
        far_junk = Polygon([(100_000, 100_000), (100_010, 100_000), (100_010, 100_010), (100_000, 100_010)])
        layers = [
            _layer(id="territory", kind="zone", object_type="territory", geometry=shape_to_db(TERRITORY)),
            _layer(id="territory-junk", kind="zone", object_type="territory", geometry=shape_to_db(far_junk)),
        ]
        result = render_layer_raster(layers, source_crs=None)

        territory_group = next(g for g in result.groups if g.key == "territory")
        assert territory_group.count == 1
        (south, west), (north, east) = result.bounds
        # The canvas is anchored on the real (kept) piece, not stretched out
        # to also cover the discarded one 100km away.
        assert east < 100_000

    def test_a_stray_far_fragment_on_another_layer_does_not_balloon_the_canvas(self):
        """Same idea as the zoning test above, but for a non-zoning group:
        live case, 4. Харьковская улица, where "unknown" alone had a
        fragment spanning tens of kilometres and "utility"/"lighting_pole"
        each had one straggler thousands of metres past the real site --
        anchoring the canvas on the union of every layer let any single one
        of these silently set the frame for everything else.
        """
        stray_utility = LineString([(50_000, 50_000), (50_010, 50_000)])
        layers = [
            _layer(id="territory", kind="zone", object_type="territory", geometry=shape_to_db(TERRITORY)),
            _layer(id="stray", kind="utility", object_type="gas_pipe", geometry=shape_to_db(stray_utility)),
        ]
        result = render_layer_raster(layers, source_crs=None)

        (south, west), (north, east) = result.bounds
        assert east < 50_000

    def test_existing_greenery_and_existing_lawn_always_get_a_legend_row(self):
        """Live complaint: on one real street existing_lawn genuinely has
        zero objects (its "Газон" layers are the project's OWN proposed
        design, correctly left unclassified rather than guessed as real
        existing turf -- see layer_rules.py), and a silently-absent legend
        toggle for that read as "the feature is broken" rather than "this
        street's data has none". Both categories must appear even when a
        project (like this fixture) has neither, with an honest count of 0."""
        layers = [_layer(id="territory", kind="zone", object_type="territory", geometry=shape_to_db(TERRITORY))]

        result = render_layer_raster(layers, source_crs=None)

        by_key = {g.key: g for g in result.groups}
        assert by_key["existing_greenery"].count == 0
        assert by_key["existing_lawn"].count == 0
        # Still a real, validly-decodable (fully transparent) PNG, not a
        # placeholder/None -- the frontend's <ImageOverlay> renders whatever
        # this returns unconditionally once the group is in the list.
        image = Image.open(__import__("io").BytesIO(by_key["existing_lawn"].png))
        assert image.mode == "RGBA"
        assert image.getextrema()[3] == (0, 0), "an empty group's canvas must be fully transparent, not garbage pixels"

    def test_a_stray_fragment_does_not_balloon_the_canvas_when_there_is_no_territory_at_all(self):
        """Live case, "7. Нижние Поля ул": no closeable boundary anywhere in
        the bundle (MissingTerritoryError), so render_layer_raster has no
        territory to anchor bounds on at all and falls back to the union of
        every other layer -- and on that street, several categories
        ("unknown", "existing_greenery", "building", "lighting_pole") each
        carried content thousands of metres from the real site (not near the
        origin, so drop_origin doesn't catch it), stretching the canvas to a
        useless ~9x13km span. No `territory` layer in this fixture at all --
        _dominant_cluster_bounds must still keep the real, densely-clustered
        mass and drop the one far-off straggler.
        """
        real_site = [Point(x, y) for x in range(0, 100, 10) for y in range(0, 100, 10)]
        stray = Point(50_000, 50_000)
        layers = [
            _layer(id=f"real-{i}", kind="zone", object_type="existing_greenery", geometry=shape_to_db(p))
            for i, p in enumerate(real_site)
        ] + [_layer(id="stray", kind="zone", object_type="unknown", geometry=shape_to_db(stray))]

        result = render_layer_raster(layers, source_crs=None)

        (south, west), (north, east) = result.bounds
        assert east < 1_000, "the far-off straggler must not have set the canvas frame"
        assert west > -1_000

    def test_reprojects_bounds_when_source_crs_is_set(self):
        layers = [_layer(id="territory", kind="zone", object_type="territory", geometry=shape_to_db(TERRITORY))]
        result = render_layer_raster(layers, source_crs="EPSG:32637")

        (south, west), (north, east) = result.bounds
        # UTM 37N's origin latitude is the equator -- this territory's raw
        # 0..100m northing reprojects to a sliver right at 0N. Not asserting
        # exact longitude (that's pyproj's job, and UTM 37N's 500km false
        # easting puts even a 0m easting well east of 0E -- see this
        # session's Kenya-map finding in CLAUDE.md); the point of this test
        # is only that a transform actually ran instead of raw metres
        # passing straight through as if they were already degrees.
        assert -1 < south < north < 1
        assert not (-1 < west < east < 1)


class TestParallelRendering:
    """Real projects (>=_PARALLEL_RENDER_THRESHOLD geometries) render each
    legend group in its own process instead of one thread doing them in
    sequence -- see _render_groups_parallel's docstring. Every test above
    this class stays well under the threshold, so none of them ever
    exercises that path; this forces it via monkeypatch against the same
    tiny fixtures, on the assumption that a real ProcessPoolExecutor
    round-trip (ie. not mocked) is the only way to actually catch a
    pickling mistake."""

    def test_parallel_path_matches_sequential_path_byte_for_byte(self, monkeypatch):
        layers = [
            _layer(id="territory", kind="zone", object_type="territory", geometry=shape_to_db(TERRITORY)),
            _layer(id="gas", kind="utility", object_type="gas_pipe", geometry=shape_to_db(GAS_PIPE)),
            _layer(id="building", kind="zone", object_type="building", geometry=shape_to_db(BUILDING)),
        ]

        sequential = render_layer_raster(layers, source_crs=None)

        monkeypatch.setattr(layer_raster_module, "_PARALLEL_RENDER_THRESHOLD", 1)
        parallel = render_layer_raster(layers, source_crs=None)

        assert {g.key for g in parallel.groups} == {g.key for g in sequential.groups}
        assert {g.key: g.png for g in parallel.groups} == {g.key: g.png for g in sequential.groups}
        assert parallel.bounds == sequential.bounds

    def test_single_group_never_spins_up_a_pool(self, monkeypatch):
        """render_layer_raster also requires len(by_group) > 1 before using
        the pool -- a project with only one legend group would otherwise pay
        process-spawn overhead for a "parallel" render of exactly one thing.
        Forcing the threshold down here should still take the inline path,
        not crash trying to divide work across a pool of one.

        The always-shown existing_greenery/existing_lawn pair (see
        render_layer_raster's own comment) means a real call always has at
        least 3 groups now, so this can't reach `len(by_group) == 1` through
        the public function any more -- kept anyway as a smoke test that a
        handful of mostly-empty groups still renders without touching the
        pool-of-one edge case in _render_groups_parallel."""
        layers = [_layer(id="territory", kind="zone", object_type="territory", geometry=shape_to_db(TERRITORY))]
        monkeypatch.setattr(layer_raster_module, "_PARALLEL_RENDER_THRESHOLD", 1)

        result = render_layer_raster(layers, source_crs=None)

        assert {g.key for g in result.groups} == {"territory", "existing_greenery", "existing_lawn"}
