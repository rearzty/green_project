"""Unit tests for edit_service.py that don't need a real PostGIS connection:
_violates_setback/from_wgs84 (pure functions), and the planting_type/geometry
mismatch guard exercised against transient (un-persisted) ORM objects with a
fake AsyncSession standing in for a real one -- apply_item_patch/
apply_structured_edit only ever call add/commit/refresh/delete on it.
"""

from __future__ import annotations

import asyncio

import pytest
from shapely.geometry import Point, Polygon, box, mapping

from backend.app.db.models import Plan, PlantingItemRow
from backend.app.services.edit_service import (
    PlantingTypeGeometryMismatchError,
    _violates_setback,
    apply_item_patch,
    apply_structured_edit,
)
from backend.app.services.geo_io import _to_wgs84, from_wgs84, shape_to_db

SQUARE = Polygon([(0, 0), (0, 10), (10, 10), (10, 0)])


def test_lawn_polygon_touching_exclusion_boundary_is_not_a_violation():
    """buffers.buildable_area() carves lawn footprints out as exactly
    territory.difference(exclusion_zone), so a lawn item's boundary always
    touches the exclusion zone's boundary by construction -- that must not
    count as a violation (regression for the false positive every generated
    lawn plan used to trigger on /validate).
    """
    exclusion = box(10, 0, 20, 10)
    lawn = box(0, 0, 10, 10)  # shares the x=10 edge with exclusion, no overlap
    assert not _violates_setback(lawn, exclusion)


def test_lawn_polygon_actually_overlapping_exclusion_is_a_violation():
    exclusion = box(5, 0, 20, 10)
    lawn = box(0, 0, 10, 10)  # overlaps exclusion in [5, 10]
    assert _violates_setback(lawn, exclusion)


def test_point_inside_exclusion_is_a_violation():
    exclusion = box(0, 0, 10, 10)
    assert _violates_setback(Point(5, 5), exclusion)


def test_point_outside_exclusion_is_not_a_violation():
    exclusion = box(0, 0, 10, 10)
    assert not _violates_setback(Point(50, 50), exclusion)


def test_from_wgs84_is_the_inverse_of_to_wgs84():
    """A manual edit's geometry/click coordinates arrive from Leaflet in
    WGS84 -- from_wgs84() must land back where a point in the project's own
    CRS actually is, or dragged/drawn edits silently drift off every other
    stored geometry (this was a live no-op bug before source_crs was ever
    set on a real project; EPSG:32637 now is, by default, in the UI).
    """
    original = Point(414732.85, 6180932.32)  # a real EPSG:32637 point
    wgs84 = _to_wgs84(original, "EPSG:32637")
    back = from_wgs84(wgs84, "EPSG:32637")
    assert original.distance(back) < 1e-6


def test_from_wgs84_passthrough_without_source_crs():
    point = Point(1, 2)
    assert from_wgs84(point, None) is point


class _FakeAsyncSession:
    """Stands in for a SQLAlchemy AsyncSession so these tests don't need a
    real PostGIS connection -- edit_service only ever calls
    add/commit/refresh/delete on the session."""

    def add(self, obj):
        pass

    async def commit(self):
        pass

    async def refresh(self, obj):
        pass

    async def delete(self, obj):
        pass


def _point_item(planting_type: str = "shrub") -> PlantingItemRow:
    item = PlantingItemRow(
        id="item-point",
        plan_id="plan-1",
        geometry=shape_to_db(Point(5, 5)),
        planting_type=planting_type,
        species="default",
    )
    item.plan = Plan(id="plan-1", project_id="project-1")
    return item


def _polygon_item(planting_type: str = "lawn") -> PlantingItemRow:
    item = PlantingItemRow(
        id="item-polygon",
        plan_id="plan-1",
        geometry=shape_to_db(SQUARE),
        planting_type=planting_type,
        species="default",
    )
    item.plan = Plan(id="plan-1", project_id="project-1")
    return item


class TestApplyItemPatch:
    def test_retyping_point_item_to_lawn_is_rejected(self):
        item = _point_item(planting_type="shrub")

        with pytest.raises(PlantingTypeGeometryMismatchError):
            asyncio.run(
                apply_item_patch(session=_FakeAsyncSession(), item=item, geometry=None, planting_type="lawn", species=None, source_crs=None)
            )

        assert item.planting_type == "shrub"

    def test_retyping_polygon_item_to_tree_is_rejected(self):
        item = _polygon_item(planting_type="lawn")

        with pytest.raises(PlantingTypeGeometryMismatchError):
            asyncio.run(
                apply_item_patch(session=_FakeAsyncSession(), item=item, geometry=None, planting_type="tree", species=None, source_crs=None)
            )

        assert item.planting_type == "lawn"

    def test_setting_point_geometry_on_a_lawn_item_is_rejected(self):
        item = _polygon_item(planting_type="lawn")
        point_geojson = mapping(Point(1, 1))

        with pytest.raises(PlantingTypeGeometryMismatchError):
            asyncio.run(
                apply_item_patch(
                    session=_FakeAsyncSession(), item=item, geometry=point_geojson, planting_type=None, species=None, source_crs=None
                )
            )

    def test_retyping_point_item_between_tree_and_shrub_is_allowed(self):
        item = _point_item(planting_type="shrub")

        updated = asyncio.run(
            apply_item_patch(session=_FakeAsyncSession(), item=item, geometry=None, planting_type="tree", species=None, source_crs=None)
        )

        assert updated.planting_type == "tree"
        assert item.plan.has_manual_edits is True


class TestApplyStructuredEditReplaceTypeInZone:
    def test_replace_type_in_zone_rejects_point_items_when_target_is_lawn(self):
        plan = Plan(id="plan-1", project_id="project-1")
        plan.items = [_point_item(planting_type="shrub")]

        with pytest.raises(PlantingTypeGeometryMismatchError):
            asyncio.run(
                apply_structured_edit(
                    session=_FakeAsyncSession(),
                    plan=plan,
                    operation="replace_type_in_zone",
                    params={"polygon": mapping(SQUARE), "planting_type": "lawn"},
                    source_crs=None,
                )
            )

        assert plan.items[0].planting_type == "shrub"

    def test_replace_type_in_zone_rejects_polygon_items_when_target_is_tree(self):
        plan = Plan(id="plan-1", project_id="project-1")
        plan.items = [_polygon_item(planting_type="lawn")]

        with pytest.raises(PlantingTypeGeometryMismatchError):
            asyncio.run(
                apply_structured_edit(
                    session=_FakeAsyncSession(),
                    plan=plan,
                    operation="replace_type_in_zone",
                    params={"polygon": mapping(SQUARE), "planting_type": "tree"},
                    source_crs=None,
                )
            )

        assert plan.items[0].planting_type == "lawn"

    def test_replace_type_in_zone_allows_matching_geometry_kind(self):
        plan = Plan(id="plan-1", project_id="project-1")
        plan.items = [_point_item(planting_type="shrub")]

        asyncio.run(
            apply_structured_edit(
                session=_FakeAsyncSession(),
                plan=plan,
                operation="replace_type_in_zone",
                params={"polygon": mapping(SQUARE), "planting_type": "tree"},
                source_crs=None,
            )
        )

        assert plan.items[0].planting_type == "tree"
