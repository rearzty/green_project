"""Conversions between DB rows (GeoAlchemy2 geometry columns), geo_engine's
plain shapely-based domain objects, and the GeoJSON the frontend speaks.
"""

from __future__ import annotations

import uuid
from functools import lru_cache

import numpy as np
import shapely
from geoalchemy2.shape import from_shape, to_shape
from pyproj import Transformer
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform

from backend.app.db.models import Layer, PlantingItemRow
from backend.app.schemas.geo import GeoJSONFeature, GeoJSONFeatureCollection, GeoJSONGeometry
from geo_engine.model import PlantingItem, Utility, Zone


def shape_to_db(geometry: BaseGeometry):
    return from_shape(geometry, srid=0)


def db_to_shape(geometry) -> BaseGeometry:
    return to_shape(geometry)


@lru_cache(maxsize=32)
def _transformer_to_wgs84(source_crs: str) -> Transformer:
    return Transformer.from_crs(source_crs, "EPSG:4326", always_xy=True)


@lru_cache(maxsize=32)
def _transformer_from_wgs84(source_crs: str) -> Transformer:
    return Transformer.from_crs("EPSG:4326", source_crs, always_xy=True)


def _to_wgs84(geometry: BaseGeometry, source_crs: str | None) -> BaseGeometry:
    """Reproject a single geometry to WGS84 for GeoJSON/Leaflet output.

    `source_crs` is the uploading project's declared CRS (optional form
    field) — the real Mosgeotrest CRS is unknown until 2026-09-15, so most
    projects today have none set. Without a known source CRS there is no
    correct way to place raw local-unit coordinates on a real-world map, so
    they're passed through unprojected rather than guessed; the frontend
    fits the map view to whatever extent it actually gets instead of
    assuming a fixed real-world center.
    """
    if not source_crs:
        return geometry
    return shapely_transform(_transformer_to_wgs84(source_crs).transform, geometry)


def from_wgs84(geometry: BaseGeometry, source_crs: str | None) -> BaseGeometry:
    """Inverse of `_to_wgs84` — reproject a geometry the frontend sent in
    WGS84 (Leaflet's native CRS: a dragged marker, a click-drawn edit
    polygon) back into the project's own `source_crs` before storing it or
    comparing it against other stored geometry, which is *not* WGS84 whenever
    source_crs is set. Same no-op passthrough as `_to_wgs84` when it's unset.
    """
    if not source_crs:
        return geometry
    return shapely_transform(_transformer_from_wgs84(source_crs).transform, geometry)


def layer_to_domain(layer: Layer) -> Utility | Zone:
    geometry = db_to_shape(layer.geometry)
    if layer.kind == "utility":
        return Utility(geometry=geometry, object_type=layer.object_type, layer_source=str(layer.id), attrs=layer.attrs or {})
    return Zone(geometry=geometry, zone_type=layer.object_type, attrs=layer.attrs or {})


def layers_to_domain(layers: list[Layer]) -> tuple[list[Utility], list[Zone]]:
    utilities: list[Utility] = []
    zones: list[Zone] = []
    for layer in layers:
        domain = layer_to_domain(layer)
        if isinstance(domain, Utility):
            utilities.append(domain)
        else:
            zones.append(domain)
    return utilities, zones


def layer_to_geojson_feature(layer: Layer, source_crs: str | None = None) -> GeoJSONFeature:
    geometry = _to_wgs84(db_to_shape(layer.geometry), source_crs)
    return GeoJSONFeature(
        geometry=GeoJSONGeometry(**mapping(geometry)),
        properties={"kind": layer.kind, "object_type": layer.object_type, **(layer.attrs or {})},
    )


def layers_to_feature_collection(layers: list[Layer], source_crs: str | None = None) -> GeoJSONFeatureCollection:
    return GeoJSONFeatureCollection(features=[layer_to_geojson_feature(l, source_crs) for l in layers])


def planting_item_to_geojson_feature(item: PlantingItemRow, source_crs: str | None = None) -> GeoJSONFeature:
    geometry = _to_wgs84(db_to_shape(item.geometry), source_crs)
    return GeoJSONFeature(
        geometry=GeoJSONGeometry(**mapping(geometry)),
        properties={
            "id": item.id,
            "planting_type": item.planting_type,
            "species": item.species,
            "score": item.score,
            "rationale": item.rationale,
            "is_manual_edit": item.is_manual_edit,
        },
    )


def _to_wgs84_batch(geoms: np.ndarray, source_crs: str | None) -> np.ndarray:
    """Reproject a whole array of geometries in one shot -- one C-level
    pyproj.Transformer.transform() call over every coordinate at once,
    instead of the Python-level shapely.ops.transform()-per-geometry
    `_to_wgs84` does (measured ~300x faster at 20k geometries: 0.33us/item
    vectorized vs 103us/item one at a time, which dominates the cost of
    building a plan's GeoJSON response at real scale). Geometry *structure*
    (point vs polygon, ring nesting, ...) is untouched -- only coordinate
    values change, so get_coordinates/set_coordinates round-trips every
    coordinate through the transform while preserving exactly which
    geometry and position each one belongs to.
    """
    if not source_crs or len(geoms) == 0:
        return geoms
    transformer = _transformer_to_wgs84(source_crs)
    coords = shapely.get_coordinates(geoms)
    new_x, new_y = transformer.transform(coords[:, 0], coords[:, 1])
    return shapely.set_coordinates(geoms, np.column_stack([new_x, new_y]))


def _item_properties(item: PlantingItemRow) -> dict:
    return {
        "id": item.id,
        "planting_type": item.planting_type,
        "species": item.species,
        "score": item.score,
        "rationale": item.rationale,
        "is_manual_edit": item.is_manual_edit,
    }


def _reproject_items(items: list[PlantingItemRow], source_crs: str | None) -> np.ndarray:
    geoms = np.empty(len(items), dtype=object)
    geoms[:] = [db_to_shape(item.geometry) for item in items]
    return _to_wgs84_batch(geoms, source_crs)


def planting_items_to_feature_collection(
    items: list[PlantingItemRow], source_crs: str | None = None
) -> GeoJSONFeatureCollection:
    """Vectorized counterpart of calling `planting_item_to_geojson_feature`
    per item -- see `_to_wgs84_batch`. `model_construct()` skips Pydantic
    validation on the way out: the data is built from our own trusted shapely
    geometries and DB rows, not untrusted input, so re-validating each of
    potentially hundreds of thousands of features on every plan fetch is
    pure overhead.
    """
    if not items:
        return GeoJSONFeatureCollection(features=[])

    geoms = _reproject_items(items, source_crs)
    features = [
        GeoJSONFeature.model_construct(type="Feature", geometry=GeoJSONGeometry.model_construct(**mapping(geom)), properties=_item_properties(item))
        for item, geom in zip(items, geoms)
    ]
    return GeoJSONFeatureCollection.model_construct(type="FeatureCollection", features=features)


def planting_items_to_geojson_dicts(items: list[PlantingItemRow], source_crs: str | None = None) -> list[dict]:
    """Vectorized counterpart of `[planting_item_to_geojson_feature(item, source_crs).model_dump() for item in items]`
    -- same `_to_wgs84_batch` reprojection as `planting_items_to_feature_collection`,
    but returns plain dicts instead of a FeatureCollection: edit_service's
    batch operations (delete_items/retype_items/move_items) hand these
    straight to a Pydantic response model field (`list[GeoJSONFeature]`)
    that validates them on the way out anyway, so building `GeoJSONFeature`
    instances here first would just be extra work thrown away one line
    later. Added after "Выделить всё" + move/retype/delete on a real-scale
    plan measurably hit the exact per-item reprojection cost
    `planting_items_to_feature_collection` was already fixed for elsewhere --
    this closes the same gap for the batch-edit response snapshots.
    """
    if not items:
        return []
    geoms = _reproject_items(items, source_crs)
    return [{"type": "Feature", "geometry": mapping(geom), "properties": _item_properties(item)} for item, geom in zip(items, geoms)]


def domain_item_to_row_values(plan_id: str, item: PlantingItem) -> dict:
    """Plain dict of PlantingItemRow column values, for a Core bulk INSERT
    (`sqlalchemy.insert(PlantingItemRow), rows`) rather than individually
    `session.add()`-ed ORM objects -- generate_plan can produce thousands of
    these per request, and per-object ORM tracking measured at ~2.5ms/row
    (no statement batching) where a single bulk INSERT takes a fraction of
    a second regardless of row count.
    """
    return {
        "id": str(uuid.uuid4()),
        "plan_id": plan_id,
        "geometry": shape_to_db(item.geometry),
        "planting_type": item.planting_type,
        "species": item.species,
        "score": item.score,
        "rationale": item.rationale,
        "is_manual_edit": item.is_manual_edit,
    }
