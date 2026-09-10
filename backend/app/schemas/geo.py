"""Minimal RFC 7946 GeoJSON schemas — just enough structure for FastAPI's
OpenAPI docs and validation; geometry.coordinates stays untyped `Any` since
its shape depends on geometry.type and fully modeling that is unnecessary
overhead for a hackathon backend.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class GeoJSONGeometry(BaseModel):
    type: str
    coordinates: Any


class GeoJSONFeature(BaseModel):
    type: str = "Feature"
    geometry: GeoJSONGeometry
    properties: dict[str, Any] = {}


class GeoJSONFeatureCollection(BaseModel):
    type: str = "FeatureCollection"
    features: list[GeoJSONFeature]
