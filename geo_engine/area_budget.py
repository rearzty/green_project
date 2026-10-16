"""Сколько площади участка осталось под посадку и что съело остальное.

Ответ на замечание экспертов «на некоторых данных выставляются только кусты».
Причина в таких случаях не в расстановке: на плотной улице табличные отступы
(СП 42.13330.2016 табл. 9.1, 743-ПП табл. 3.6.1) от края проезжей части и от
кабелей покрывают почти весь участок, и дереву с его двухметровыми отступами
физически негде стоять, а кустарнику с меньшими — есть. Живой замер на «17.
Грузинская М ул»: для деревьев остаётся ~40 м² из 30 270, край проезжей части
закрывает 89 % участка, кабели — 65 %. Без объяснения такой план выглядит как
сбой генератора; с ним — как честный вывод из норм, который можно проверить.

Считается по сетке точек внутри участка, а не объединением буферов: нужна
доля, а не точная фигура, и векторизованный запрос ближайшего объекта к
десяткам тысяч точек дешевле, чем ещё одно объединение буферов на каждый тип
ограничения. Отступы — те же `norms.resolve_setback`, что у генератора и
отчёта обоснований, с той же породой.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import shapely
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from geo_engine.buffers import HARD_OBSTACLE_ZONE_TYPES
from geo_engine.compliance import OBJECT_LABELS_RU
from geo_engine.model import Utility, Zone
from geo_engine.norms import PlantingNorms
from geo_engine.species import Species

# Точек на участок. 20 тысяч дают долю с точностью около процента и считаются
# за доли секунды на тип ограничения даже на крупной улице.
_MAX_SAMPLES = 20_000

TERRITORY_MARGIN_KEY = "territory_margin"
HARD_OBSTACLES_KEY = "hard_obstacles"


@dataclass
class Blocker:
    object_type: str
    label: str
    share: float  # доля площади участка, закрытая этим ограничением в одиночку


@dataclass
class AreaBudget:
    planting_type: str
    territory_m2: float
    usable_m2: float
    usable_share: float
    blockers: list[Blocker] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def summary_ru(self, limit: int = 3) -> str:
        top = ", ".join(f"{b.label} — {round(100 * b.share)} %" for b in self.blockers[:limit])
        text = (
            f"под посадку остаётся {self.usable_m2:,.0f} м² из {self.territory_m2:,.0f} "
            f"({100 * self.usable_share:.1f} % участка)"
        ).replace(",", " ")
        return f"{text}; больше всего площади закрывают: {top}" if top else text


def _sample_points(territory: BaseGeometry) -> tuple[np.ndarray, float]:
    """Точки сетки внутри участка и площадь, которую представляет каждая."""
    minx, miny, maxx, maxy = territory.bounds
    area = territory.area
    step = max(0.5, (area / _MAX_SAMPLES) ** 0.5)
    xs = np.arange(minx + step / 2, maxx, step)
    ys = np.arange(miny + step / 2, maxy, step)
    gx, gy = np.meshgrid(xs, ys)
    gx, gy = gx.ravel(), gy.ravel()
    shapely.prepare(territory)
    inside = shapely.contains_xy(territory, gx, gy)
    points = shapely.points(gx[inside], gy[inside])
    return points, (area / len(points) if len(points) else 0.0)


def _blocked_by(points: np.ndarray, geometries: list[BaseGeometry], distance: float) -> np.ndarray:
    """Какие точки ближе `distance` к хоть одному из объектов (внутри — тоже)."""
    if not geometries or len(points) == 0:
        return np.zeros(len(points), dtype=bool)
    tree = STRtree(geometries)
    if distance <= 0:
        hits = tree.query(points, predicate="intersects")
    else:
        hits = tree.query(points, predicate="dwithin", distance=distance)
    mask = np.zeros(len(points), dtype=bool)
    mask[np.unique(hits[0])] = True
    return mask


def area_budget(
    territory: BaseGeometry,
    utilities: list[Utility],
    zones: list[Zone],
    planting_type: str,
    norms: PlantingNorms,
    species: Species | None = None,
    crown_reference_diameter_m: float = 5.0,
) -> AreaBudget | None:
    if territory is None or territory.is_empty or territory.area <= 0:
        return None
    points, cell_m2 = _sample_points(territory)
    if len(points) == 0:
        return None

    by_type: dict[str, list[BaseGeometry]] = {}
    for utility in utilities:
        by_type.setdefault(utility.object_type, []).append(utility.geometry)
    for zone in zones:
        if zone.zone_type in norms.setbacks_m:
            by_type.setdefault(zone.zone_type, []).append(zone.geometry)

    masks: dict[str, np.ndarray] = {}
    for object_type, geometries in by_type.items():
        required = norms.resolve_setback(object_type, planting_type, species, crown_reference_diameter_m).required_m
        masks[object_type] = _blocked_by(points, [g for g in geometries if g is not None and not g.is_empty], required)

    margin = norms.territory_margin_for(planting_type)
    if margin > 0:
        boundary = territory.boundary
        shapely.prepare(boundary)
        masks[TERRITORY_MARGIN_KEY] = shapely.dwithin(boundary, points, margin)
    hard = [
        z.geometry
        for z in zones
        if z.zone_type in HARD_OBSTACLE_ZONE_TYPES and z.geometry.geom_type in ("Polygon", "MultiPolygon")
    ]
    masks[HARD_OBSTACLES_KEY] = _blocked_by(points, hard, 0.0)

    blocked = np.zeros(len(points), dtype=bool)
    for mask in masks.values():
        blocked |= mask
    labels = {
        **OBJECT_LABELS_RU,
        TERRITORY_MARGIN_KEY: "отступ от границы участка",
        HARD_OBSTACLES_KEY: "существующие здания, покрытия и зелень",
    }
    blockers = sorted(
        (
            Blocker(object_type=key, label=labels.get(key, key), share=float(mask.mean()))
            for key, mask in masks.items()
            if mask.any()
        ),
        key=lambda b: -b.share,
    )
    usable = int((~blocked).sum())
    return AreaBudget(
        planting_type=planting_type,
        territory_m2=float(territory.area),
        usable_m2=usable * cell_m2,
        usable_share=usable / len(points),
        blockers=blockers,
    )
