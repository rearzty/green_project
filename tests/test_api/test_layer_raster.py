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
        assert keys == {"territory", "utility", "building"}
        for group in result.groups:
            assert group.count == 1
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
        """A zoning polygon can span a whole neighbourhood -- same exclusion
        as MapView.tsx's extentBounds, so the canvas frames the actual site,
        not the zoning's much bigger footprint."""
        huge_zoning = Polygon([(-10_000, -10_000), (-10_000, 10_000), (10_000, 10_000), (10_000, -10_000)])
        layers = [
            _layer(id="territory", kind="zone", object_type="territory", geometry=shape_to_db(TERRITORY)),
            _layer(id="zoning", kind="zone", object_type="zoning", geometry=shape_to_db(huge_zoning), attrs={"zoning_category": "residential"}),
        ]
        result = render_layer_raster(layers, source_crs=None)

        (south, west), (north, east) = result.bounds
        assert (west, south) == (0, 0)
        assert (east, north) == (100, 100)

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
