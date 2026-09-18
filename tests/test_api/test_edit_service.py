"""Unit tests for edit_service.py: violation_mask/from_wgs84 (pure
functions), the planting_type/geometry mismatch guard, and the id-based
batch edits (delete/retype/move/restore), exercised against plain in-memory
records (backend.app.db.models) -- there's no database at all to stand in
for.
"""

from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import Point, Polygon, box, mapping

from backend.app.db.models import Layer, Plan, PlantingItemRow, Project
from backend.app.services import exclusion_cache
from backend.app.services.edit_service import (
    ItemsAlreadyExistError,
    ItemsNotFoundError,
    OutOfTerritoryError,
    PlantingTypeGeometryMismatchError,
    apply_item_patch,
    delete_items,
    move_items,
    restore_items,
    retype_items,
    validate_items,
    validate_plan,
    violation_mask,
)
from backend.app.services.geo_io import (
    _to_wgs84,
    db_to_shape,
    from_wgs84,
    planting_item_to_geojson_feature,
    planting_items_to_geojson_dicts,
    shape_to_db,
)

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
    assert not violation_mask([lawn], exclusion)[0]


def test_lawn_polygon_actually_overlapping_exclusion_is_a_violation():
    exclusion = box(5, 0, 20, 10)
    lawn = box(0, 0, 10, 10)  # overlaps exclusion in [5, 10]
    assert violation_mask([lawn], exclusion)[0]


def test_point_inside_exclusion_is_a_violation():
    exclusion = box(0, 0, 10, 10)
    assert violation_mask([Point(5, 5)], exclusion)[0]


def test_point_outside_exclusion_is_not_a_violation():
    exclusion = box(0, 0, 10, 10)
    assert not violation_mask([Point(50, 50)], exclusion)[0]


def test_violation_mask_handles_mixed_points_and_polygons_in_one_call():
    exclusion = box(0, 0, 10, 10)
    mask = violation_mask([Point(5, 5), Point(50, 50), box(5, 0, 20, 10), box(10, 0, 20, 10)], exclusion)
    assert mask.tolist() == [True, False, True, False]


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


def test_batch_snapshot_reprojection_matches_the_single_item_path():
    """planting_items_to_geojson_dicts (edit_service's batch delete/retype/
    move snapshots) reprojects a whole array of geometries in one vectorized
    call instead of one at a time -- every existing test above exercises it
    only with source_crs=None, which short-circuits before that code path
    runs at all. Cross-checks it against the known-correct single-item
    planting_item_to_geojson_feature for a batch of real EPSG:32637 points
    (including a polygon, to make sure a lawn's multi-point geometry survives
    the same coordinate round-trip)."""
    items = [
        _row("a", Point(414732.85, 6180932.32), "tree"),
        _row("b", Point(414800.0, 6181000.0), "shrub"),
        _row("c", box(414700.0, 6180900.0, 414750.0, 6180950.0), "lawn"),
    ]

    batch = planting_items_to_geojson_dicts(items, "EPSG:32637")
    singles = [planting_item_to_geojson_feature(item, "EPSG:32637").model_dump() for item in items]

    assert [f["properties"]["id"] for f in batch] == [f["properties"]["id"] for f in singles]
    for b, s in zip(batch, singles):
        assert b["geometry"]["type"] == s["geometry"]["type"]
        b_coords = np.array(b["geometry"]["coordinates"], dtype=float)
        s_coords = np.array(s["geometry"]["coordinates"], dtype=float)
        assert np.allclose(b_coords, s_coords, atol=1e-9)


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
            apply_item_patch(item=item, geometry=None, planting_type="lawn", species=None, source_crs=None)

        assert item.planting_type == "shrub"

    def test_retyping_polygon_item_to_tree_is_rejected(self):
        item = _polygon_item(planting_type="lawn")

        with pytest.raises(PlantingTypeGeometryMismatchError):
            apply_item_patch(item=item, geometry=None, planting_type="tree", species=None, source_crs=None)

        assert item.planting_type == "lawn"

    def test_setting_point_geometry_on_a_lawn_item_is_rejected(self):
        item = _polygon_item(planting_type="lawn")
        point_geojson = mapping(Point(1, 1))

        with pytest.raises(PlantingTypeGeometryMismatchError):
            apply_item_patch(item=item, geometry=point_geojson, planting_type=None, species=None, source_crs=None)

    def test_retyping_point_item_between_tree_and_shrub_is_allowed(self):
        item = _point_item(planting_type="shrub")

        updated = apply_item_patch(item=item, geometry=None, planting_type="tree", species=None, source_crs=None)

        assert updated.planting_type == "tree"
        assert item.plan.has_manual_edits is True


def _item_in_territory(territory: Polygon, point: Point, planting_type: str = "shrub") -> PlantingItemRow:
    item = PlantingItemRow(
        id="item-point",
        plan_id="plan-1",
        geometry=shape_to_db(point),
        planting_type=planting_type,
        species="default",
    )
    project = Project(id="project-1", name="test")
    project.layers = [
        Layer(id="layer-territory", project_id="project-1", kind="zone", object_type="territory", geometry=shape_to_db(territory))
    ]
    plan = Plan(id="plan-1", project_id="project-1")
    plan.project = project
    item.plan = plan
    return item


class TestApplyItemPatchTerritoryBoundary:
    def test_moving_a_point_outside_the_territory_is_rejected(self):
        item = _item_in_territory(territory=SQUARE, point=Point(5, 5))
        outside_point = mapping(Point(50, 50))

        with pytest.raises(OutOfTerritoryError):
            apply_item_patch(item=item, geometry=outside_point, planting_type=None, species=None, source_crs=None)

    def test_moving_a_point_within_the_territory_is_allowed(self):
        item = _item_in_territory(territory=SQUARE, point=Point(1, 1))
        inside_point = mapping(Point(8, 8))

        updated = apply_item_patch(item=item, geometry=inside_point, planting_type=None, species=None, source_crs=None)

        assert db_to_shape(updated.geometry).equals(Point(8, 8))


BIG_TERRITORY = Polygon([(0, 0), (0, 100), (100, 100), (100, 0)])


def _plan_with_territory(territory: Polygon) -> Plan:
    project = Project(id="project-1", name="test")
    project.layers = [
        Layer(id="layer-territory", project_id="project-1", kind="zone", object_type="territory", geometry=shape_to_db(territory))
    ]
    plan = Plan(id="plan-1", project_id="project-1")
    plan.project = project
    return plan


class TestRestoreItems:
    def test_restores_an_item_with_the_same_id_and_data(self):
        plan = _plan_with_territory(BIG_TERRITORY)
        item = _point_item(planting_type="tree")
        snapshot = planting_item_to_geojson_feature(item, None).model_dump()
        plan.item_count = 5

        restore_items(plan=plan, features=[snapshot], source_crs=None)

        assert plan.item_count == 6
        assert plan.has_manual_edits is True

    def test_restored_rows_keep_the_snapshots_own_manual_edit_flag(self):
        """Undoing a delete puts items back exactly as they were -- a generated
        item must not come back marked as a manual edit."""
        plan = _plan_with_territory(BIG_TERRITORY)
        plan.item_count = 0
        generated = planting_item_to_geojson_feature(_row("gen", Point(1, 1), "tree"), None).model_dump()
        edited_row = _row("edited", Point(2, 2), "tree")
        edited_row.is_manual_edit = True
        edited = planting_item_to_geojson_feature(edited_row, None).model_dump()

        restore_items(plan=plan, features=[generated, edited], source_crs=None)

        assert {item.id: item.is_manual_edit for item in plan.items} == {"gen": False, "edited": True}

    def test_rejects_a_snapshot_with_mismatched_type_and_geometry(self):
        plan = _plan_with_territory(BIG_TERRITORY)
        item = _point_item(planting_type="tree")
        snapshot = planting_item_to_geojson_feature(item, None).model_dump()
        snapshot["properties"]["planting_type"] = "lawn"  # Point geometry, lawn type -- mismatch

        with pytest.raises(PlantingTypeGeometryMismatchError):
            restore_items(plan=plan, features=[snapshot], source_crs=None)

    def test_rejects_a_snapshot_positioned_outside_the_territory(self):
        plan = _plan_with_territory(SQUARE)  # tight territory, [0,10]x[0,10]
        snapshot = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [50.0, 50.0]},
            "properties": {"id": "item-x", "planting_type": "tree", "species": "default", "score": 0.5, "rationale": "test"},
        }

        with pytest.raises(OutOfTerritoryError):
            restore_items(plan=plan, features=[snapshot], source_crs=None)

    def test_restoring_a_lawn_touching_the_territory_boundary_survives_a_real_crs_round_trip(self):
        """Reproduces a live bug: delete_items snapshots an item's geometry
        reprojected source_crs -> WGS84 (geo_io.planting_items_to_geojson_dicts),
        and restore_items reprojects it straight back -- every other test in
        this class uses source_crs=None, which skips that reprojection
        entirely and never exercises the round trip it does in production.
        With a real source_crs the round trip reintroduces a tiny bit of
        floating-point noise (see _assert_all_within_territory's docstring),
        and a lawn polygon carved out by buffers.buildable_area() always
        touches the territory boundary somewhere by construction --
        restoring it unmoved must not be rejected as OutOfTerritoryError.
        """
        territory = box(414000.0, 6180000.0, 414100.0, 6180100.0)  # real EPSG:32637-scale coords
        lawn = box(414000.0, 6180000.0, 414050.0, 6180050.0)  # shares two edges with the territory boundary
        plan = _plan_with_items(territory, [], project_id="restore-crs-roundtrip-project")
        item = _row("lawn-1", lawn, "lawn")
        snapshot = planting_item_to_geojson_feature(item, "EPSG:32637").model_dump()

        restore_items(plan=plan, features=[snapshot], source_crs="EPSG:32637")

        assert plan.item_count == 1

    def test_restore_still_rejects_a_genuinely_out_of_territory_item_with_a_real_crs(self):
        """Same real-CRS round trip as above, but the item actually is
        outside the territory (not just nudged by reprojection noise) -- the
        tolerance _assert_all_within_territory now applies must not swallow
        real violations."""
        territory = box(414000.0, 6180000.0, 414100.0, 6180100.0)
        far_outside = box(415000.0, 6181000.0, 415050.0, 6181050.0)
        plan = _plan_with_items(territory, [], project_id="restore-crs-roundtrip-rejects-project")
        item = _row("lawn-2", far_outside, "lawn")
        snapshot = planting_item_to_geojson_feature(item, "EPSG:32637").model_dump()

        with pytest.raises(OutOfTerritoryError):
            restore_items(plan=plan, features=[snapshot], source_crs="EPSG:32637")


def _plan_with_items(territory: Polygon, items: list[PlantingItemRow], project_id: str = "project-1") -> Plan:
    plan = _plan_with_territory(territory)
    plan.project.id = project_id
    plan.item_count = len(items)
    plan.items = items
    return plan


def _row(item_id: str, geometry, planting_type: str) -> PlantingItemRow:
    return PlantingItemRow(id=item_id, plan_id="plan-1", geometry=shape_to_db(geometry), planting_type=planting_type, species="default")


class TestBatchEditsById:
    """The selection tool and local undo/redo edit by explicit item id --
    unlike a zone/area, re-running one of these later acts on exactly the
    same items, whatever has moved into or out of an area since."""

    def test_delete_items_returns_snapshots_of_exactly_the_requested_ids(self):
        plan = _plan_with_items(BIG_TERRITORY, [_row("a", Point(1, 1), "tree"), _row("b", Point(2, 2), "shrub"), _row("c", Point(3, 3), "tree")])

        snapshots = delete_items(plan=plan, ids=["a", "c", "a"], source_crs=None)

        assert [s["properties"]["id"] for s in snapshots] == ["a", "c"]  # duplicates collapsed, order kept
        assert plan.item_count == 1
        assert plan.has_manual_edits is True

    def test_batch_edit_with_unknown_id_is_rejected_before_anything_changes(self):
        plan = _plan_with_items(BIG_TERRITORY, [_row("a", Point(1, 1), "tree")])

        with pytest.raises(ItemsNotFoundError):
            move_items(plan=plan, ids=["a", "gone"], from_point=(0, 0), to_point=(1, 1), source_crs=None)

        assert db_to_shape(plan.items[0].geometry).equals(Point(1, 1))

    def test_retype_items_skips_geometry_incompatible_items_and_reports_only_real_changes(self):
        lawn = _row("lawn", box(20, 20, 30, 30), "lawn")
        plan = _plan_with_items(BIG_TERRITORY, [_row("a", Point(1, 1), "shrub"), _row("b", Point(2, 2), "tree"), lawn])

        previous, skipped = retype_items(plan=plan, changes=[("a", "tree"), ("b", "tree"), ("lawn", "tree")], source_crs=None)

        assert [p["properties"]["id"] for p in previous] == ["a"]  # "b" already a tree -> not a change
        assert previous[0]["properties"]["planting_type"] == "shrub"  # snapshot holds the pre-change type
        assert skipped == ["lawn"]
        assert [i.planting_type for i in plan.items] == ["tree", "tree", "lawn"]

    def test_retype_items_restores_each_items_own_previous_type(self):
        plan = _plan_with_items(BIG_TERRITORY, [_row("a", Point(1, 1), "tree"), _row("b", Point(2, 2), "tree")])

        retype_items(plan=plan, changes=[("a", "shrub"), ("b", "tree")], source_crs=None)

        assert [i.planting_type for i in plan.items] == ["shrub", "tree"]

    def test_move_items_translates_only_the_given_ids_and_its_inverse_undoes_it(self):
        plan = _plan_with_items(BIG_TERRITORY, [_row("a", Point(1, 1), "tree"), _row("b", Point(50, 50), "tree")])

        moved = move_items(plan=plan, ids=["a"], from_point=(0, 0), to_point=(3, 4), source_crs=None)
        assert db_to_shape(plan.items[0].geometry).equals(Point(4, 5))
        assert db_to_shape(plan.items[1].geometry).equals(Point(50, 50))
        # Returns the moved items' post-move snapshots -- the frontend applies
        # these locally instead of re-fetching the whole plan.
        assert len(moved) == 1
        assert moved[0]["properties"]["id"] == "a"
        assert tuple(moved[0]["geometry"]["coordinates"]) == (4.0, 5.0)

        move_items(plan=plan, ids=["a"], from_point=(3, 4), to_point=(0, 0), source_crs=None)
        assert db_to_shape(plan.items[0].geometry).equals(Point(1, 1))

    def test_move_items_is_all_or_nothing_at_the_territory_boundary(self):
        plan = _plan_with_items(SQUARE, [_row("a", Point(1, 1), "tree"), _row("b", Point(9, 9), "tree")])

        with pytest.raises(OutOfTerritoryError):
            move_items(plan=plan, ids=["a", "b"], from_point=(0, 0), to_point=(2, 0), source_crs=None)

        assert db_to_shape(plan.items[0].geometry).equals(Point(1, 1))  # "a" would still fit, but nothing moved

    def test_restore_refuses_ids_that_are_already_in_the_plan(self):
        item = _row("a", Point(1, 1), "tree")
        plan = _plan_with_items(BIG_TERRITORY, [item])
        snapshot = planting_item_to_geojson_feature(item, None).model_dump()

        with pytest.raises(ItemsAlreadyExistError):
            restore_items(plan=plan, features=[snapshot], source_crs=None)


class TestValidateItems:
    """validate_items is validate_plan restricted to specific ids -- what an
    edit actually touched -- used by the frontend to avoid a full plan's
    worth of checks after every single edit."""

    def test_only_checks_the_requested_ids(self, monkeypatch):
        monkeypatch.setattr(exclusion_cache, "build_exclusion_zone", lambda utilities, zones, planting_type, norms: box(0, 0, 10, 10))
        exclusion_cache._cache.clear()
        plan = _plan_with_items(
            BIG_TERRITORY,
            [_row("violating", Point(5, 5), "tree"), _row("clean", Point(50, 50), "tree"), _row("unchecked", Point(5, 5), "tree")],
            project_id="validate-items-project-1",
        )

        violations = validate_items(plan.project, plan, ["violating", "clean"])

        # "unchecked" sits inside the same exclusion zone as "violating" --
        # it's absent from the result only because it wasn't asked for.
        assert {v["item_id"] for v in violations} == {"violating"}

    def test_matches_validate_plan_when_given_every_id(self, monkeypatch):
        monkeypatch.setattr(exclusion_cache, "build_exclusion_zone", lambda utilities, zones, planting_type, norms: box(0, 0, 10, 10))
        exclusion_cache._cache.clear()
        items = [_row("a", Point(5, 5), "tree"), _row("b", Point(50, 50), "tree")]
        plan = _plan_with_items(BIG_TERRITORY, items, project_id="validate-items-project-2")

        full = validate_plan(plan.project, plan)
        partial = validate_items(plan.project, plan, [i.id for i in items])

        assert full == partial


def test_exclusion_zone_is_built_once_per_project_and_type(monkeypatch):
    calls = []

    def fake_build(utilities, zones, planting_type, norms):
        calls.append(planting_type)
        return box(0, 0, 1, 1)

    monkeypatch.setattr(exclusion_cache, "build_exclusion_zone", fake_build)
    project = _plan_with_territory(BIG_TERRITORY).project
    project.id = "cache-test-project"

    first = exclusion_cache.with_exclusion_zone(project, "tree", lambda zone: zone)
    second = exclusion_cache.with_exclusion_zone(project, "tree", lambda zone: zone)
    exclusion_cache.with_exclusion_zone(project, "shrub", lambda zone: zone)

    assert first is second
    assert calls == ["tree", "shrub"]


def test_territory_is_looked_up_once_per_project(monkeypatch):
    """_assert_all_within_territory (every move/restore/patch edit) goes
    through exclusion_cache.with_territory -- confirms it doesn't re-parse
    project layers on every call."""
    exclusion_cache._territory_cache.clear()
    calls = []
    plan = _plan_with_items(BIG_TERRITORY, [_row("a", Point(1, 1), "tree")], project_id="territory-cache-project")
    real_layers_to_domain = exclusion_cache.layers_to_domain

    def counting_layers_to_domain(layers):
        calls.append(1)
        return real_layers_to_domain(layers)

    monkeypatch.setattr(exclusion_cache, "layers_to_domain", counting_layers_to_domain)

    move_items(plan=plan, ids=["a"], from_point=(0, 0), to_point=(1, 1), source_crs=None)
    move_items(plan=plan, ids=["a"], from_point=(1, 1), to_point=(2, 2), source_crs=None)

    assert len(calls) == 1
