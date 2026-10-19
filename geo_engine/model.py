"""Domain model for the geo_engine pipeline. Pure dataclasses — no FastAPI/DB deps."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from shapely.geometry.base import BaseGeometry

PlantingType = Literal["tree", "shrub", "lawn"]


@dataclass
class Utility:
    """An underground/overhead engineering network object that imposes a setback."""

    geometry: BaseGeometry
    object_type: str  # e.g. "heat_network", "water_pipe", "gas_pipe", "cable_line"
    layer_source: str = ""
    attrs: dict = field(default_factory=dict)


@dataclass
class Zone:
    """A non-utility spatial constraint or context object: building, road,
    zoning polygon, existing greenery, territory outline, etc."""

    geometry: BaseGeometry
    zone_type: str  # e.g. "building", "road", "zoning", "existing_greenery", "territory"
    attrs: dict = field(default_factory=dict)


@dataclass
class PlantingCandidate:
    """A location where a planting of a given type could legally go, prior to scoring."""

    geometry: BaseGeometry  # Point for tree/shrub, Polygon for lawn
    planting_type: PlantingType
    clearance_m: float  # distance to the nearest exclusion boundary
    area_m2: float | None = None  # relevant for lawn candidates
    zoning: str | None = None
    # К какой группе посадок относится место: ряд вдоль одного ориентира, одна
    # куртина, одна группа деревьев. None — россыпь и солитер (у них группы нет).
    # Нужно интерфейсу: «Выделить похожие → весь ряд» выделяет ровно точки одной
    # группы, а не «всё рядом по расстоянию».
    group: str | None = None


@dataclass
class PlantingItem:
    """A candidate that was selected (or manually placed/edited) into a final plan."""

    geometry: BaseGeometry
    planting_type: PlantingType
    species: str
    score: float
    rationale: str
    is_manual_edit: bool = False
    # Идентификатор группы посадок (см. PlantingCandidate.group); у россыпи и
    # солитера None. Стабилен между пересчётами одного плана: id выводятся из
    # номера ориентира и положения ядра, а не из счётчика, который пережил бы
    # прошлую генерацию в этом же процессе.
    group_id: str | None = None
