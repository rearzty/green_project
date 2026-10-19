"""Прогноз «Ожидается» на вкладке «План» (п. 8 редизайна): сколько посадок
выйдет при выбранных настройках — до генерации и меньше чем за секунду.

Настоящий расчёт (ряды вдоль ориентиров, каскад пород, куртины) занимает
секунды-минуты и сам зависит от настроек нелинейно, поэтому прогноз — не его
упрощённая копия, а масштабирование. Два режима, и честная метка у каждого
(`basis` в ответе):

* **`plan` — от открытого плана.** У проекта уже есть план с известными числами.
  Меняется только интервал, число рядов или тип — и ответ это число,
  умноженное на отношение «как реагирует расстановка на такую смену». У
  кустарника реакция точная по построению: изгородь идёт по линии с шагом, так
  что число посадок обратно пропорционально шагу. У деревьев — степенная
  зависимость от шага, подобранная по замерам. Систематическая ошибка модели
  (длина ориентиров, породы) сокращается, потому что общий множитель берётся
  из самого плана.
* **`area` — по площади.** Плана ещё нет. Число из допустимой площади
  (`geo_engine.area_budget`, та же, что в «Почему деревьев мало») и усреднённой
  «стоимости» одной посадки в квадратных метрах. Грубо: на длину изгородей и
  рядов влияет форма участка, которой по одной площади не узнать, поэтому
  диапазон широкий, а в интерфейсе прямо сказано, что после первой
  генерации прогноз точнее.

Константы ниже подобраны по ОДНОЙ улице («2. Песчаный переулок», десять
рецептов — таблица в CLAUDE.md, «Прогноз генерации»): на ней они дают ошибку в
пределах диапазона, но это подгонка, а не проверка — на других улицах точность
не измерена, и это сказано в документации, а не в интерфейсе.
"""

from __future__ import annotations

import threading
from collections import Counter, OrderedDict
from dataclasses import dataclass

from backend.app.core.config import settings
from backend.app.db.models import Plan, Project
from backend.app.schemas.plan import GenerateRequest
from backend.app.services import exclusion_cache
from backend.app.services.geo_io import db_to_shape, layers_to_domain
from geo_engine.area_budget import area_budget
from geo_engine.norms import load_norms
from geo_engine.planner import IN_ROW_PITCH_M
from geo_engine.territory import MissingTerritoryError
from geo_engine.species import load_catalogue

# Эффективный шаг ряда деревьев при интервале «по умолчанию» (по классам
# кроны, каскад от крупной к мелкой): итог по «Песчаному» — 155 деревьев, а
# явный интервал 4 м даёт 145, то есть до ~4 м пользовательский интервал почти
# ничего не меняет (нижняя граница задаётся нормой класса кроны).
TREE_BASE_PITCH_M = 4.0
# Число деревьев ∝ шаг^−0,75: рядом с препятствиями ряд обрывается, поэтому
# зависимость слабее 1/шаг (замер: 4→145, 6→112, 8→85, 11→70).
TREE_PITCH_EXPONENT = 0.75
# Двухрядная аллея на той же улице дала 110 против 155 (семь-восемь метров
# между деревьями при двух рядах не окупаются теснотой у бордюра).
TREE_DOUBLE_ROW_FACTOR = 0.71
SHRUB_DEFAULT_PITCH_M = IN_ROW_PITCH_M["shrub"]
# Доля допустимой площади, которая в итоге становится газоном: газон режется по
# препятствиям, остаётся ~93 % (замер: 26 923 → 25 152 м²).
LAWN_FILL = 0.93

# Запас вокруг ответа: от плана — десяток процентов, по площади — почти вдвое.
SPREAD_FROM_PLAN = 0.12
SPREAD_FROM_PLAN_UNKNOWN = 0.2  # деревья в покрытии: эффект зависит от тротуаров, на замере его не было
SPREAD_FROM_AREA = 0.45
SPREAD_LAWN = 0.08

# По площади: при базовом шаге дерево занимает 1,09·p² допустимой площади,
# изгородь — одну погонную единицу на 3 м² допустимой площади кустарника. Обе —
# подгонка.
FALLBACK_TREE_M2_PER_PITCH_SQ = 1.09
FALLBACK_SHRUB_METRES_PER_M2 = 1 / 3

_MAX_CACHED = 6
_cache: "OrderedDict[str, _Prepared]" = OrderedDict()
_lock = threading.Lock()


@dataclass
class _Prepared:
    """Слои проекта в доменном виде и территория: перевод тысяч слоёв стоит
    секунд, а прогноз пересчитывается при каждой правке настроек."""

    utilities: list
    zones: list
    territory: object


def _prepared(project: Project) -> _Prepared:
    key = str(project.id)
    with _lock:
        found = _cache.get(key)
        if found is not None:
            _cache.move_to_end(key)
            return found
    utilities, zones = layers_to_domain(project.layers)
    territory = exclusion_cache.with_territory(project, lambda t: t)
    prepared = _Prepared(utilities=utilities, zones=zones, territory=territory)
    with _lock:
        _cache[key] = prepared
        while len(_cache) > _MAX_CACHED:
            _cache.popitem(last=False)
    return prepared


def _range(value: float, spread: float) -> tuple[int, int]:
    return max(0, int(round(value * (1 - spread)))), int(round(value * (1 + spread)))


def _tree_pitch(spacing_m: float | None) -> float:
    return max(spacing_m or 0.0, TREE_BASE_PITCH_M)


def _rows_factor(rows: int) -> float:
    return TREE_DOUBLE_ROW_FACTOR if rows >= 2 else 1.0


def _shrub_pitch(spacing_m: float | None) -> float:
    return spacing_m or SHRUB_DEFAULT_PITCH_M


def _anchor(project: Project) -> Plan | None:
    """План, от которого масштабируем: текущий, если у него есть посадки."""
    return next((p for p in project.plans if p.is_current and p.items), None)


def _usable_m2(project: Project, planting_type: str) -> float | None:
    """Допустимая площадь под тип посадки: из плана, если он есть (уже
    посчитана при генерации), иначе та же `area_budget` по слоям проекта.
    None — оценить не из чего (у проекта нет границы участка)."""
    for plan in project.plans:
        for entry in plan.area_budget or []:
            if entry.get("planting_type") == planting_type:
                return float(entry["usable_m2"])
    try:
        prepared = _prepared(project)
    except MissingTerritoryError:
        return None
    norms = load_norms(settings.planting_norms_path)
    catalogue = load_catalogue()
    budget = area_budget(prepared.territory, prepared.utilities, prepared.zones, planting_type, norms, None, catalogue.crown_reference_diameter_m)
    return None if budget is None else float(budget.usable_m2)


def estimate(project: Project, request: GenerateRequest) -> dict:
    """Диапазоны по каждому запрошенному типу посадки. `unit` — «items» (штуки)
    или «m2» (газон). Ответ без типа — если оценить не из чего (нет границы
    участка и нет плана)."""
    anchor = _anchor(project)
    counts: Counter[str] = Counter()
    lawn_area = 0.0
    if anchor is not None:
        for row in anchor.items:
            counts[row.planting_type] += 1
            if row.planting_type == "lawn":
                lawn_area += float(db_to_shape(row.geometry).area)

    estimates: list[dict] = []
    for planting_type in request.planting_types:
        if planting_type == "lawn":
            if anchor is not None and counts["lawn"] > 0:
                value, basis = lawn_area, "plan"
            else:
                usable = _usable_m2(project, "lawn")
                if usable is None:
                    continue
                value, basis = usable * LAWN_FILL, "area"
            low, high = _range(value, SPREAD_LAWN)
            estimates.append({"planting_type": "lawn", "low": low, "high": high, "unit": "m2", "basis": basis})
            continue

        if anchor is not None and counts[planting_type] > 0:
            base = counts[planting_type]
            if planting_type == "tree":
                factor = (_tree_pitch(anchor.tree_spacing_m) / _tree_pitch(request.tree_spacing_m)) ** TREE_PITCH_EXPONENT
                factor *= _rows_factor(request.tree_rows) / _rows_factor(anchor.tree_rows)
                spread = SPREAD_FROM_PLAN_UNKNOWN if request.trees_in_paving != anchor.trees_in_paving else SPREAD_FROM_PLAN
            else:
                factor = _shrub_pitch(anchor.shrub_spacing_m) / _shrub_pitch(request.shrub_spacing_m)
                spread = SPREAD_FROM_PLAN
            low, high = _range(base * factor, spread)
            estimates.append({"planting_type": planting_type, "low": low, "high": high, "unit": "items", "basis": "plan"})
            continue

        usable = _usable_m2(project, planting_type)
        if usable is None:
            continue
        if planting_type == "tree":
            # Площадь даёт число при шаге по умолчанию; смена шага — тот же
            # степенной закон, что и от плана (а не 1/p²: измерено, что число
            # деревьев на шаг отвечает слабее — ряд рвётся у препятствий).
            at_base_pitch = usable / (FALLBACK_TREE_M2_PER_PITCH_SQ * TREE_BASE_PITCH_M**2)
            value = at_base_pitch * (TREE_BASE_PITCH_M / _tree_pitch(request.tree_spacing_m)) ** TREE_PITCH_EXPONENT * _rows_factor(request.tree_rows)
        else:
            value = usable * FALLBACK_SHRUB_METRES_PER_M2 / _shrub_pitch(request.shrub_spacing_m)
        low, high = _range(value, SPREAD_FROM_AREA)
        estimates.append({"planting_type": planting_type, "low": low, "high": high, "unit": "items", "basis": "area"})
    return {"estimates": estimates}
