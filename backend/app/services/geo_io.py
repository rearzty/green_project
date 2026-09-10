"""Conversions between DB rows (GeoAlchemy2 geometry columns), geo_engine's
plain shapely-based domain objects, and the GeoJSON the frontend speaks.
"""

from __future__ import annotations

from functools import lru_cache

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


def planting_items_to_feature_collection(
    items: list[PlantingItemRow], source_crs: str | None = None
) -> GeoJSONFeatureCollection:
    return GeoJSONFeatureCollection(features=[planting_item_to_geojson_feature(i, source_crs) for i in items])


def domain_item_to_row(plan_id: str, item: PlantingItem) -> PlantingItemRow:
    return PlantingItemRow(
        plan_id=plan_id,
        geometry=shape_to_db(item.geometry),
        planting_type=item.planting_type,
        species=item.species,
        score=item.score,
        rationale=item.rationale,
        is_manual_edit=item.is_manual_edit,
    )
