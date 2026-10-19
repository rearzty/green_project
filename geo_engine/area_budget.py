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


@dataclass
class _Masks:
    """Что и в какой мере закрывает точки: маска «закрыто этим» по каждому
    ограничению, нужный отступ (м) там, где он есть, и подписи."""

    masks: dict[str, np.ndarray]
    required_m: dict[str, float]
    labels: dict[str, str]

    def blocked(self, size: int) -> np.ndarray:
        blocked = np.zeros(size, dtype=bool)
        for mask in self.masks.values():
            blocked |= mask
        return blocked


def _blocker_masks(
    points: np.ndarray,
    territory: BaseGeometry,
    utilities: list[Utility],
    zones: list[Zone],
    planting_type: str,
    norms: PlantingNorms,
    species: Species | None,
    crown_reference_diameter_m: float,
) -> _Masks:
    """Одна и та же раскладка «кто закрывает точку» — и для доли в объяснении
    площади, и для слоя на карте: числа панели и картинка не должны расходиться."""
    by_type: dict[str, list[BaseGeometry]] = {}
    for utility in utilities:
        by_type.setdefault(utility.object_type, []).append(utility.geometry)
    for zone in zones:
        if zone.zone_type in norms.setbacks_m:
            by_type.setdefault(zone.zone_type, []).append(zone.geometry)

    masks: dict[str, np.ndarray] = {}
    required_m: dict[str, float] = {}
    for object_type, geometries in by_type.items():
        required = norms.resolve_setback(object_type, planting_type, species, crown_reference_diameter_m).required_m
        required_m[object_type] = float(required)
        geometries = [g for g in geometries if g is not None and not g.is_empty]
        if object_type == "sidewalk" and norms.paving_plantable(planting_type):
            geometries = [g.boundary if g.geom_type in ("Polygon", "MultiPolygon") else g for g in geometries]
        masks[object_type] = _blocked_by(points, geometries, required)

    margin = norms.territory_margin_for(planting_type)
    if margin > 0:
        boundary = territory.boundary
        shapely.prepare(boundary)
        masks[TERRITORY_MARGIN_KEY] = shapely.dwithin(boundary, points, margin)
        required_m[TERRITORY_MARGIN_KEY] = float(margin)
    hard = [
        z.geometry
        for z in zones
        if z.zone_type in HARD_OBSTACLE_ZONE_TYPES
        and not (z.zone_type == "sidewalk" and norms.paving_plantable(planting_type))
        and z.geometry.geom_type in ("Polygon", "MultiPolygon")
    ]
    masks[HARD_OBSTACLES_KEY] = _blocked_by(points, hard, 0.0)

    labels = {
        **OBJECT_LABELS_RU,
        TERRITORY_MARGIN_KEY: "отступ от границы участка",
        HARD_OBSTACLES_KEY: "существующие здания, покрытия и зелень",
    }
    return _Masks(masks=masks, required_m=required_m, labels=labels)


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

    found = _blocker_masks(points, territory, utilities, zones, planting_type, norms, species, crown_reference_diameter_m)
    blocked = found.blocked(len(points))
    blockers = sorted(
        (
            Blocker(object_type=key, label=found.labels.get(key, key), share=float(mask.mean()))
            for key, mask in found.masks.items()
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


# Слой на карте мельче сетки объяснения площади: там нужна доля (20 тысяч точек
# дают процент), здесь — картинка, на которой полоса теплосети в 2 м не должна
# превращаться в мозаику по 2 м. 400 тысяч ячеек — полметра на дворе в 10 га и
# метр на 40 га; запрос ближайшего объекта по ним векторизован.
_MAX_ZONE_SAMPLES = 400_000
_MIN_ZONE_CELL_M = 0.5


@dataclass
class ZoneBlocker:
    key: str
    label: str
    required_m: float | None
    share: float  # доля участка, закрытая этим ограничением
    exclusive_m2: float  # площадь, закрытая ТОЛЬКО им — освободится, если его убрать
    mask: np.ndarray = field(repr=False)


@dataclass
class ZoneMasks:
    """Растр «где можно / что закрыло» на сетке с началом в (minx, maxy):
    строка 0 — верх (север), как у картинки. Маски — bool[height, width]."""

    minx: float
    miny: float
    maxx: float
    maxy: float
    cell_m: float
    territory_m2: float
    usable_m2: float
    usable_share: float
    allowed: np.ndarray = field(repr=False)
    blockers: list[ZoneBlocker] = field(default_factory=list)


def area_zone_masks(
    territory: BaseGeometry,
    utilities: list[Utility],
    zones: list[Zone],
    planting_type: str,
    norms: PlantingNorms,
    species: Species | None = None,
    crown_reference_diameter_m: float = 5.0,
    max_samples: int = _MAX_ZONE_SAMPLES,
) -> ZoneMasks | None:
    """Те же ограничения, что в `area_budget`, но растром по всей рамке участка —
    для слоя «где можно сажать» (картинка на карте)."""
    if territory is None or territory.is_empty or territory.area <= 0:
        return None
    minx, miny, maxx, maxy = territory.bounds
    cell = max(_MIN_ZONE_CELL_M, (territory.area / max_samples) ** 0.5)
    xs = minx + cell / 2 + cell * np.arange(int(np.ceil((maxx - minx) / cell)))
    ys = maxy - cell / 2 - cell * np.arange(int(np.ceil((maxy - miny) / cell)))  # сверху вниз
    height, width = len(ys), len(xs)
    if height == 0 or width == 0:
        return None
    gx, gy = np.meshgrid(xs, ys)
    flat_x, flat_y = gx.ravel(), gy.ravel()
    shapely.prepare(territory)
    inside = shapely.contains_xy(territory, flat_x, flat_y)
    if not inside.any():
        return None
    points = shapely.points(flat_x[inside], flat_y[inside])

    found = _blocker_masks(points, territory, utilities, zones, planting_type, norms, species, crown_reference_diameter_m)
    blocked = found.blocked(len(points))
    cell_m2 = cell * cell

    def scatter(values: np.ndarray) -> np.ndarray:
        grid = np.zeros(height * width, dtype=bool)
        grid[np.flatnonzero(inside)] = values
        return grid.reshape(height, width)

    count = len(points)
    # Сколько ограничений закрывает каждую точку: «только это» — ровно одно.
    cover = np.zeros(count, dtype=np.int32)
    for mask in found.masks.values():
        cover += mask
    blockers = sorted(
        (
            ZoneBlocker(
                key=key,
                label=found.labels.get(key, key),
                required_m=found.required_m.get(key),
                share=float(mask.mean()),
                exclusive_m2=float((mask & (cover == 1)).sum() * cell_m2),
                mask=scatter(mask),
            )
            for key, mask in found.masks.items()
            if mask.any()
        ),
        key=lambda b: -b.share,
    )
    usable = int((~blocked).sum())
    return ZoneMasks(
        minx=minx,
        miny=miny,
        maxx=maxx,
        maxy=maxy,
        cell_m=cell,
        territory_m2=float(territory.area),
        usable_m2=float(territory.area) * usable / count,
        usable_share=usable / count,
        allowed=scatter(~blocked),
        blockers=blockers,
    )
