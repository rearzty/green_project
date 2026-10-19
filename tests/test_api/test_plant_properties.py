"""Свойства посадки в GeoJSON, которыми пользуются карта и 3D-просмотр:
диаметр кроны у дерева и куста (3D рисует каждое растение своего размера),
класс кроны — только у дерева (по нему меняется размер точки на карте)."""

from __future__ import annotations

from shapely.geometry import Polygon

from backend.app.db.models import PlantingItemRow
from backend.app.services.geo_io import planting_item_to_geojson_feature, planting_items_to_geojson_dicts, shape_to_db
from shapely.geometry import Point


def _row(planting_type: str, species: str, geometry=None) -> PlantingItemRow:
    return PlantingItemRow(plan_id="p", geometry=shape_to_db(geometry or Point(1, 1)), planting_type=planting_type, species=species)


def test_a_tree_carries_its_species_crown_diameter_and_class():
    props = planting_item_to_geojson_feature(_row("tree", "Дуб черешчатый"), None).properties

    assert props["crown_m"] == 10.0 and props["crown"] == "wide"


def test_a_shrub_now_carries_its_crown_diameter_too_but_no_crown_class():
    props = planting_item_to_geojson_feature(_row("shrub", "Сирень обыкновенная"), None).properties

    assert props["crown_m"] == 2.5
    assert props["crown"] is None, "класс кроны задаёт размер ТОЧКИ дерева на карте, у куста его нет"


def test_a_lawn_has_no_crown():
    props = planting_item_to_geojson_feature(_row("lawn", "default", Polygon([(0, 0), (4, 0), (4, 4)])), None).properties

    assert props["crown_m"] is None


def test_the_batch_snapshot_path_agrees_with_the_single_item_path():
    rows = [_row("tree", "Липа мелколистная"), _row("shrub", "Барбарис Тунберга")]

    batch = [f["properties"]["crown_m"] for f in planting_items_to_geojson_dicts(rows, None)]
    single = [planting_item_to_geojson_feature(r, None).properties["crown_m"] for r in rows]

    assert batch == single == [8.0, 0.8]


def test_an_unknown_species_has_no_diameter_rather_than_a_made_up_one():
    props = planting_item_to_geojson_feature(_row("tree", "Неизвестная порода"), None).properties

    assert props["crown_m"] is None
