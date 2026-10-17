"""3D получает итоговый участок и только окрестность — см. scene3d_service."""

from __future__ import annotations

from shapely.geometry import Polygon, box, shape

from backend.app.db.models import Layer, Project
from backend.app.services.geo_io import shape_to_db
from backend.app.services.scene3d_service import scene_layers


def _layer(object_type, geometry, kind="zone"):
    return Layer(project_id="p3d", kind=kind, object_type=object_type, geometry=shape_to_db(geometry))


def _project():
    project = Project(id="scene3d-project", name="3d")
    project.layers = [
        _layer("territory", box(0, 0, 100, 80)),
        _layer("territory", box(5000, 5000, 5010, 5010)),  # дальний обрывок — участок его отбросит
        _layer("building", box(10, 10, 30, 30)),
        _layer("building", box(3000, 3000, 3020, 3020)),  # далеко от участка
        _layer("road", box(-500, 85, 600, 95)),  # длинная дорога рядом — обрезается
        _layer("building", Polygon([(40, 40), (60, 60), (60, 40), (40, 60)])),  # «бабочка», невалидна
        _layer("gas_pipe", box(0, 40, 100, 41), kind="utility"),
    ]
    return project


def test_only_the_final_territory_and_its_surroundings_reach_the_scene():
    features = scene_layers(_project()).features
    types = [f.properties["object_type"] for f in features]

    assert types.count("territory") == 1
    assert types.count("building") == 2  # дальнее здание отброшено
    assert "gas_pipe" not in types


def test_long_roads_are_clipped_and_invalid_polygons_repaired():
    features = scene_layers(_project()).features
    road = next(shape(f.geometry.model_dump()) for f in features if f.properties["object_type"] == "road")
    assert road.bounds[0] >= -31 and road.bounds[2] <= 131
    for f in features:
        assert shape(f.geometry.model_dump()).is_valid


def test_sliver_building_fragments_do_not_become_walls_in_3d():
    """Обрывок контура здания, раздутый на 0,3 м при чтении, в 3D дал бы
    стену-пластину высотой с дом («5. Багрицкого»: 2667 из 2734)."""
    from shapely.geometry import LineString

    project = _project()
    project.layers.append(_layer("building", LineString([(10, 50), (35, 50)]).buffer(0.3)))
    types = [f.properties["object_type"] for f in scene_layers(project).features]

    assert types.count("building") == 2
