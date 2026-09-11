"""Regression tests for the planting_type/geometry mismatch guard in
edit_service.py. These exercise apply_item_patch and apply_structured_edit
directly against transient (un-persisted) ORM objects with shapely geometry
converted through geo_io.shape_to_db, and a no-op fake in place of a real
SQLAlchemy Session -- no DB/PostGIS connection needed, matching this repo's
unit-test suite (see tests/test_api/test_routes_registered.py).
"""

from __future__ import annotations

import pytest
from shapely.geometry import Point, Polygon, mapping

from backend.app.db.models import Plan, PlantingItemRow
from backend.app.services.edit_service import (
    PlantingTypeGeometryMismatchError,
    apply_item_patch,
    apply_structured_edit,
)
from backend.app.services.geo_io import shape_to_db

SQUARE = Polygon([(0, 0), (0, 10), (10, 10), (10, 0)])


class _FakeSession:
    """Stands in for a SQLAlchemy Session so these tests don't need a real
    PostGIS connection -- edit_service only ever calls add/commit/refresh/delete."""

    def add(self, obj):
        pass

    def commit(self):
        pass

    def refresh(self, obj):
        pass

    def delete(self, obj):
        pass


def _point_item(planting_type: str = "shrub") -> PlantingItemRow:
    return PlantingItemRow(
        id="item-point",
        plan_id="plan-1",
        geometry=shape_to_db(Point(5, 5)),
        planting_type=planting_type,
        species="default",
    )


def _polygon_item(planting_type: str = "lawn") -> PlantingItemRow:
    return PlantingItemRow(
        id="item-polygon",
        plan_id="plan-1",
        geometry=shape_to_db(SQUARE),
        planting_type=planting_type,
        species="default",
    )


class TestApplyItemPatch:
    def test_retyping_point_item_to_lawn_is_rejected(self):
        item = _point_item(planting_type="shrub")

        with pytest.raises(PlantingTypeGeometryMismatchError):
            apply_item_patch(session=_FakeSession(), item=item, geometry=None, planting_type="lawn", species=None)

        assert item.planting_type == "shrub"

    def test_retyping_polygon_item_to_tree_is_rejected(self):
        item = _polygon_item(planting_type="lawn")

        with pytest.raises(PlantingTypeGeometryMismatchError):
            apply_item_patch(session=_FakeSession(), item=item, geometry=None, planting_type="tree", species=None)

        assert item.planting_type == "lawn"

    def test_setting_point_geometry_on_a_lawn_item_is_rejected(self):
        item = _polygon_item(planting_type="lawn")
        point_geojson = mapping(Point(1, 1))

        with pytest.raises(PlantingTypeGeometryMismatchError):
            apply_item_patch(session=_FakeSession(), item=item, geometry=point_geojson, planting_type=None, species=None)

    def test_retyping_point_item_between_tree_and_shrub_is_allowed(self):
        item = _point_item(planting_type="shrub")

        updated = apply_item_patch(session=_FakeSession(), item=item, geometry=None, planting_type="tree", species=None)

        assert updated.planting_type == "tree"


class TestApplyStructuredEditReplaceTypeInZone:
    def test_replace_type_in_zone_rejects_point_items_when_target_is_lawn(self):
        plan = Plan(id="plan-1", project_id="project-1")
        plan.items = [_point_item(planting_type="shrub")]

        with pytest.raises(PlantingTypeGeometryMismatchError):
            apply_structured_edit(
                session=_FakeSession(),
                plan=plan,
                operation="replace_type_in_zone",
                params={"polygon": mapping(SQUARE), "planting_type": "lawn"},
            )

        assert plan.items[0].planting_type == "shrub"

    def test_replace_type_in_zone_rejects_polygon_items_when_target_is_tree(self):
        plan = Plan(id="plan-1", project_id="project-1")
        plan.items = [_polygon_item(planting_type="lawn")]

        with pytest.raises(PlantingTypeGeometryMismatchError):
            apply_structured_edit(
                session=_FakeSession(),
                plan=plan,
                operation="replace_type_in_zone",
                params={"polygon": mapping(SQUARE), "planting_type": "tree"},
            )

        assert plan.items[0].planting_type == "lawn"

    def test_replace_type_in_zone_allows_matching_geometry_kind(self):
        plan = Plan(id="plan-1", project_id="project-1")
        plan.items = [_point_item(planting_type="shrub")]

        apply_structured_edit(
            session=_FakeSession(),
            plan=plan,
            operation="replace_type_in_zone",
            params={"polygon": mapping(SQUARE), "planting_type": "tree"},
        )

        assert plan.items[0].planting_type == "tree"
