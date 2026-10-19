"""Слой «где нормы разрешают сажать» (п. 3 задания на редизайн): растр
допустимой площади и растры зон, закрытых каждым ограничением, для плана и типа
посадки.

Считает `geo_engine.area_budget.area_zone_masks` — те же ограничения, что в
объяснении площади («Почему деревьев мало»), только растром по всей рамке участка.
Картинки рисуются по запросу из закэшированных масок: маска — bool-массив
(сотни килобайт), PNG из неё — миллисекунды, а на пересчёт масок уходят секунды,
и повторять их на каждую переключённую галочку нельзя.

Кэш — в памяти процесса, как все производные от слоёв проекта (exclusion_cache):
слои после загрузки не меняются, породу и нормы плана (`plan_norms`) даёт сам план.
"""

from __future__ import annotations

import io
import threading
from collections import Counter, OrderedDict
from dataclasses import dataclass

import numpy as np
import shapely
from PIL import Image

from backend.app.db.models import Plan
from backend.app.services import exclusion_cache
from backend.app.services.geo_io import display_crs, layers_to_domain
from backend.app.services.layer_raster import _wgs84_bounds
from backend.app.services.pipeline_service import plan_norms
from geo_engine.area_budget import HARD_OBSTACLES_KEY, TERRITORY_MARGIN_KEY, ZoneMasks, area_zone_masks
from geo_engine.species import load_catalogue

ALLOWED_KEY = "allowed"

# RGBA слоя на карте — из макета (Zones.dc.html, спека §2): можно сажать
# зелёным, теплосеть лиловым, кабели синим, стены зданий жёлтым. Прочим типам —
# приглушённые тона, чтобы слой читался, но не спорил с четырьмя главными.
_ALLOWED_RGBA = (123, 211, 137, 87)
_BLOCKER_RGBA: dict[str, tuple[int, int, int, int]] = {
    "heat_network": (169, 148, 232, 107),
    "cable_line": (110, 180, 240, 82),
    "building": (240, 180, 90, 56),
    "gas_pipe": (232, 196, 138, 80),
    "water_pipe": (90, 170, 220, 80),
    "sewer": (180, 140, 100, 80),
    "road": (169, 177, 171, 72),
    "sidewalk": (169, 177, 171, 56),
    "sidewalk_kerb": (169, 177, 171, 56),
    "lighting_pole": (200, 182, 138, 80),
    "power_line_corridor": (232, 120, 120, 70),
    TERRITORY_MARGIN_KEY: (236, 238, 234, 46),
    HARD_OBSTACLES_KEY: (140, 150, 145, 80),
}
_DEFAULT_RGBA = (140, 150, 145, 70)

_MAX_CACHE = 8


@dataclass
class _Entry:
    masks: ZoneMasks
    interval_m: float
    pngs: dict[str, bytes]


_cache: OrderedDict[tuple[str, str], _Entry] = OrderedDict()
_lock = threading.Lock()


def _hex(rgba: tuple[int, int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgba[:3])


def _blocker_label(label: str, required_m: float | None, key: str) -> str:
    text = label[:1].upper() + label[1:]
    if required_m and key != HARD_OBSTACLES_KEY:
        text += f" {required_m:g}".replace(".", ",") + " м"
    return text


def _entry(plan: Plan, planting_type: str) -> _Entry | None:
    key = (plan.id, planting_type)
    with _lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit
    project = plan.project
    utilities, zones = layers_to_domain(project.layers)
    # Копия участка: подготовленную геометрию из кэша нельзя читать из двух
    # потоков сразу (см. exclusion_cache), а здесь её читают долго.
    territory = exclusion_cache.with_territory(project, lambda t: shapely.from_wkb(shapely.to_wkb(t)))
    names = Counter(row.species for row in plan.items if row.planting_type == planting_type).most_common(1)
    catalogue = load_catalogue()
    species = catalogue.get(names[0][0]) if names else None
    norms = plan_norms(plan)
    masks = area_zone_masks(territory, utilities, zones, planting_type, norms, species, catalogue.crown_reference_diameter_m)
    if masks is None:
        return None
    interval = norms.spacing_for(planting_type).min_distance_m
    if species is not None and planting_type == "tree":
        interval = float(catalogue.spacing_for_crown(species.crown) or interval)
    entry = _Entry(masks=masks, interval_m=float(interval), pngs={})
    with _lock:
        _cache[key] = entry
        while len(_cache) > _MAX_CACHE:
            _cache.popitem(last=False)
    return entry


def zones_summary(plan: Plan, planting_type: str) -> dict | None:
    """Доли, подписи, цвета и рамка слоя; `advice` — какое ограничение, будь оно
    снято, освободит больше всего места, и сколько посадок при текущем
    интервале там встало бы (оценка по площади, не обещание)."""
    entry = _entry(plan, planting_type)
    if entry is None:
        return None
    m = entry.masks
    bounds = _wgs84_bounds(m.minx, m.miny, m.maxx, m.maxy, display_crs(plan.project))
    groups = [
        {
            "key": ALLOWED_KEY,
            "kind": "allowed",
            "label": "Можно сажать",
            "color": _hex(_ALLOWED_RGBA),
            "share": m.usable_share,
            "required_m": None,
            "exclusive_m2": 0.0,
        }
    ]
    for blocker in m.blockers:
        groups.append(
            {
                "key": blocker.key,
                "kind": "blocker",
                "label": _blocker_label(blocker.label, blocker.required_m, blocker.key),
                "color": _hex(_BLOCKER_RGBA.get(blocker.key, _DEFAULT_RGBA)),
                "share": blocker.share,
                "required_m": blocker.required_m,
                "exclusive_m2": blocker.exclusive_m2,
            }
        )
    advice = None
    best = max(m.blockers, key=lambda b: b.exclusive_m2, default=None)
    if best is not None and best.exclusive_m2 > 0 and entry.interval_m > 0:
        advice = {
            "key": best.key,
            "label": _blocker_label(best.label, best.required_m, best.key),
            "exclusive_m2": best.exclusive_m2,
            # Квадратная раскладка с этим интервалом — верхняя прикидка, а не
            # проект: настоящая расстановка идёт рядами и теряет на отступах.
            "extra_items": int(best.exclusive_m2 // (entry.interval_m**2)),
        }
    return {
        "planting_type": planting_type,
        "bounds": bounds,
        "territory_m2": m.territory_m2,
        "usable_m2": m.usable_m2,
        "usable_share": m.usable_share,
        "interval_m": entry.interval_m,
        "groups": groups,
        "advice": advice,
    }


def zone_png(plan: Plan, planting_type: str, group: str) -> bytes | None:
    """Прозрачный PNG одной группы слоя: ячейки маски — цветом группы."""
    entry = _entry(plan, planting_type)
    if entry is None:
        return None
    with _lock:
        cached = entry.pngs.get(group)
    if cached is not None:
        return cached
    if group == ALLOWED_KEY:
        mask, rgba = entry.masks.allowed, _ALLOWED_RGBA
    else:
        blocker = next((b for b in entry.masks.blockers if b.key == group), None)
        if blocker is None:
            return None
        mask, rgba = blocker.mask, _BLOCKER_RGBA.get(blocker.key, _DEFAULT_RGBA)
    pixels = np.zeros((*mask.shape, 4), dtype=np.uint8)
    pixels[mask] = rgba
    buffer = io.BytesIO()
    Image.fromarray(pixels, mode="RGBA").save(buffer, format="PNG", optimize=True)
    png = buffer.getvalue()
    with _lock:
        entry.pngs[group] = png
    return png
