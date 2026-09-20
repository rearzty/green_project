"""Пайплайн размещения: территория и ограничения -> отобранные посадки.

Чистая геометрия и скоринг, без базы и веб-фреймворка — в этом и смысл. Раньше
этот цикл жил внутри `pipeline_service._compute_planting_rows`, то есть
единственным способом запустить ядро проекта было поднять PostGIS и FastAPI.
ТЗ просит воспроизводимый пайплайн DXF → DXF и прямо принимает CLI как способ
сдачи, поэтому алгоритм живёт здесь, а оба вызывающих — CLI и backend —
гоняют один и тот же код.
"""

from __future__ import annotations

import math
import os
import random
import zlib
from collections.abc import Callable, Collection
from concurrent.futures import ProcessPoolExecutor

from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from geo_engine.buffers import build_exclusion_zone, buildable_area
from geo_engine.candidates import ExclusionIndex, ZoningIndex, generate_candidates
from geo_engine.model import PlantingItem, Utility, Zone
from geo_engine.norms import PlantingNorms
from geo_engine.patterns import (
    GROUP_PITCH_M,
    GROUP_PLANTING_TYPES,
    GROUP_RADIUS_M,
    ROW_PLANTING_TYPES,
    collect_row_guides,
    fill_group,
    group_positions,
    row_candidates,
    territory_guide,
)
from geo_engine.placement import greedy_select
from geo_engine.species import Species, SpeciesCatalogue, load_catalogue

# Below this combined utilities+zones count, spinning up worker processes
# costs more than it saves -- each one pays its own shapely/pydantic import
# on top of pickling the utilities/zones/territory across the process
# boundary, on the order of a few hundred ms, dwarfing the actual work on a
# synthetic test territory (a handful of utilities/zones). Real deliveries
# are on a completely different scale (45,944 utilities + 331,140 zones on
# "1. Олимпийская деревня", see CLAUDE.md) where plan_items measured as 245
# of a 305s total CLI run -- the same kind of measured real-vs-synthetic
# split as dxf_writer.py's _PARALLEL_EXPORT_THRESHOLD.
_PARALLEL_PLAN_THRESHOLD = 500

# Интервал по классу кроны (МГСН 1.02-02, п. 4.2.9.2) выводится только для
# деревьев. Для кустарника 743-ПП, табл. 3.6.2 даёт 0,3-1,0 м — но это
# расстояния ВНУТРИ рядовой или групповой посадки (куртины), а генератор
# разбрасывает кусты независимыми точками. Подставить сюда 0,3 м означало бы
# применить нормативное число к чужому паттерну посадки и получить сплошной
# ковёр — ровно то, что в этом проекте уже однажды чинили, подняв интервал с
# 1,0 до 3,0 м. Групповая посадка кустарника — отдельная задача, пока не
# реализована, и до тех пор интервал кустарника берётся из planting_norms.yaml.
CROWN_SPACING_TYPES = ("tree",)

# Как расставлять посадки. "auto" — ряд вдоль линейных ориентиров плюс россыпь
# в оставшейся площади; так выглядит настоящий проект: аллея вдоль проезда и
# свободные группы во дворе. "scatter" — только россыпь, прежнее поведение,
# оставлено как способ воспроизвести старый результат и как запасной путь для
# площадок без единого линейного ориентира.
PLACEMENT_PATTERNS = ("auto", "scatter", "row", "group")

# По этой приставке в rationale CLI и отчёт отличают рядовую посадку от
# россыпи. Обоснование выбора места у них разное, и смешивать их в отчёте
# значило бы потерять единственный признак, по которому видно схему.
ROW_RATIONALE_PREFIX = "Рядовая посадка вдоль линейного ориентира."
GROUP_RATIONALE_PREFIX = "Групповая посадка (куртина)."

# Интервал внутри куртины. 743-ПП, табл. 3.6.2: «групповая посадка
# кустарников — 0,3 м». Берётся 0,5 м, а не 0,3: таблица даёт ориентир для
# рядовой и групповой посадки вперемешку, а 515-ПП табл. 5 задаёт плотность
# 1-3 шт/м², чему 0,5 м (около 4 шт/м² по сетке, меньше после прореживания
# кругами крон) соответствует ближе, чем 0,3 (около 11 шт/м²).
IN_GROUP_PITCH_M = 0.5

# Интервал внутри рядовой посадки (живой изгороди) для типов, чей обычный
# интервал (`type_norms.spacing_for`) настроен под другой паттерн, а не под
# ряд. У кустарника это ровно так: `shrub_default` (3,0 м) специально завышен,
# чтобы россыпь по всей площади не превращалась в сплошной ковёр (см.
# DEFAULT_DENSITY_PER_HA ниже и историю в CLAUDE.md) — применённый к ряду, тот
# же интервал дал бы дырявую, нехарактерную изгородь вместо плотной, которую
# 743-ПП табл. 3.6.2 отводит однорядной посадке кустарника (0,5-1 м для
# высокого класса, 0,3-0,4 для среднего/низкого). Берётся нижняя граница
# высокого класса — та же логика выбора числа из диапазона, что и везде в
# проекте: для интервала («не реже X») нижняя граница гарантирует, что норму
# невозможно нарушить, при этом остаётся достаточно плотной, чтобы читаться
# именно изгородью, а не редкой цепочкой кустов.
#
# Найдено сравнением с реальным пилотным проектом (2. Песчаный переулок,
# 20. Макеева С. ул.) — там почти весь кустарник посажен именно однорядной
# изгородью с интервалом такого порядка, не куртиной, которую генератор до
# этой правки ставил безальтернативно (см. patterns.GROUP_PLANTING_TYPES).
#
# Дерево сюда не входит: у него обычный интервал (МГСН 1.02-02 п. 4.2.9.2,
# выведенный из класса кроны) и есть верный интервал ряда/аллеи — заменять
# его не нужно.
IN_ROW_PITCH_M: dict[str, float] = {"shrub": 0.5}

# Плотность посадки по умолчанию, шт/га — практика, не норматив (акты задают
# плотность ВНУТРИ группы, но не то, сколько посадок нужно на весь участок).
# Раньше здесь сознательно не было значения: без ограничения greedy_select
# заполняет каждое легально доступное место ("сколько влезает", а не "сколько
# нужно") — на эталонной улице это дало 3892 позиции на 4,6 га сплошной
# массой, где не читались ни ряды, ни куртины. Замерено там же:
# `--density tree=25 --density shrub=250` — 1200 позиций, ряд вдоль здания
# читается как ряд, между куртинами есть воздух. Эти же числа взяты сюда
# дефолтом, а не выдуманы заново. С паттернами (ряд/куртины, см. ниже) то же
# отсутствие ограничения дало ещё нагляднее: 235 куртин без ограничения —
# 12577 кустов на одном плане, и это не только нереалистично (ни один
# проектировщик не сажает куртину на куртине через каждые 14 м по всей
# площади), но и было основным источником вычислительной нагрузки на
# реальном масштабе (см. CLAUDE.md, куртины кустарника). `plan_items()`
# подставляет эти значения только для типов, для которых вызывающий не
# передал свои — явный `density_per_ha={"shrub": 500}` меняет только shrub,
# `0` для типа отключает ограничение для него же (см. `_limit_by_density`).
DEFAULT_DENSITY_PER_HA: dict[str, float] = {"tree": 25.0, "shrub": 250.0}

# Буфер shapely — многоугольник, ВПИСАННЫЙ в окружность: его стороны-хорды
# проходят ближе к центру, чем сам радиус, поэтому точка, легшая на границу
# такого буфера, оказывается ближе требуемого расстояния. Этот дефект в проекте
# ловят уже третий раз (buffers._BUFFER_QUAD_SEGS, placement._CANOPY_BUFFER_QUAD_SEGS),
# но там хватало поднять число сегментов — здесь не хватает: при quad_segs=32
# недобор всё ещё 2,4 мм на 8 м, а compliance сравнивает с допуском 1 мм, и
# нарушение прошло бы на пустом месте. Поэтому радиус ещё и компенсируется
# точно: у правильного N-угольника, вписанного в окружность радиуса R,
# ближайшая к центру точка границы лежит на R·cos(π/N), значит буфер радиуса
# r/cos(π/N) заведомо содержит окружность радиуса r. N = 4·quad_segs.
_CLEARANCE_QUAD_SEGS = 32
_CLEARANCE_INFLATION = 1.0 / math.cos(math.pi / (4 * _CLEARANCE_QUAD_SEGS))


def _fit_row_species(
    palette: list[Species],
    planting_type: str,
    zones: list[Zone],
    territory: BaseGeometry,
    utilities: list[Utility],
    norms: PlantingNorms,
    keep_spacing_for: Collection[str],
    catalogue: SpeciesCatalogue,
    row_pitch_override_m: float | None = None,
):
    """Выбрать из палитры породу, которой на этой площадке реально есть место.

    Возвращает (порода, нормы под неё, кандидаты ряда, ориентиры).

    Нужно потому, что от класса кроны зависит и интервал (МГСН 1.02-02
    п. 4.2.9.2: 8-10 м широкая против 3-4 узкая), и величина отступов
    (примечание 1 к таблицам 743-ПП и СП: табличные значения увеличиваются
    для кроны крупнее 5 м). На плотной улице широкая крона даёт единицы
    посадок: замерено — дуб с кроной 10 м дал 14 деревьев там, где узкая
    даёт сотни. Жребий тут неуместен: проектировщик в таком месте берёт
    породу поуже, и это ровно то решение, которое инструмент может принять
    сам, потому что критерий объективен.

    Перебор по палитре, а не оптимизация: пород в палитре единицы, каждая
    проверка — один проход построения ряда.

    `row_pitch_override_m` — см. `planner.IN_ROW_PITCH_M`: для типов, чей
    обычный интервал настроен не под ряд (сейчас — кустарник), шаг ряда
    берётся отсюда, а не из `type_norms.spacing_for`. Не применяется, если
    пользователь сам задал интервал этому типу (`keep_spacing_for`) — явная
    настройка пользователя не должна тихо подменяться дефолтом изгороди.
    """
    best = None
    for candidate_species in palette or [None]:
        type_norms = norms
        if candidate_species is not None and planting_type not in keep_spacing_for:
            type_norms = norms_for_species(norms, planting_type, candidate_species, catalogue)
        if row_pitch_override_m is not None and planting_type not in keep_spacing_for:
            type_norms = type_norms.with_spacing_override(planting_type, row_pitch_override_m)
        exclusion = build_exclusion_zone(
            utilities, zones, planting_type, type_norms, candidate_species, catalogue.crown_reference_diameter_m
        )
        buildable = buildable_area(
            territory, exclusion, zones, territory_margin_m=type_norms.territory_margin_for(planting_type)
        )
        guides = collect_row_guides(
            zones, planting_type, type_norms, candidate_species, catalogue.crown_reference_diameter_m
        )
        boundary = territory_guide(territory, planting_type, type_norms)
        if boundary is not None:
            guides.append(boundary)
        rows = row_candidates(
            guides,
            buildable,
            exclusion,
            planting_type,
            type_norms.spacing_for(planting_type).min_distance_m,
            zoning_zones=zones,
        )
        if best is None or len(rows) > len(best[2]):
            best = (candidate_species, type_norms, rows, guides)
    return best


def _other_species(palette: list[Species], besides: Species | None) -> Species | None:
    """Первая порода палитры, не совпадающая с уже выбранной."""
    for candidate in palette:
        if besides is None or candidate.name != besides.name:
            return candidate
    return besides


def _palette_pick(palette: list[Species], index: int) -> Species | None:
    """Порода номер `index` палитры, по кругу.

    Детерминированно и без сида: сид уже определил саму палитру и её порядок,
    второй источник случайности тут только мешал бы — соседние куртины должны
    чередоваться предсказуемо, а не совпадать по случайности. Именно так и
    вышло в первой версии: фаза россыпи выбрала ту же породу, что и ряд,
    потому что `(1 + seed) % 3` совпало с нулём, и весь план оказался
    одновидовым при палитре из трёх.
    """
    if not palette:
        return None
    return palette[index % len(palette)]


def _subtract_item_footprints(
    area: BaseGeometry, items: list[PlantingItem], clearance_m: float
) -> BaseGeometry:
    """Вырезать из `area` места уже поставленных `items`.

    Общий шаг для перехода между фазами одного типа посадки (ряд -> куртина,
    ряд -> россыпь): у каждого прохода `greedy_select` свой пространственный
    индекс, общей памяти между фазами нет, и без явного вычитания следующая
    фаза насыпала бы новые позиции прямо на уже поставленный ряд.

    Буфер — ЦЕЛЫЙ требуемый интервал (`clearance_m`), а не половина: точка
    следующей фазы ставится в сам остаток площади, её собственный радиус
    здесь ничем не компенсирован (в отличие от отбора внутри одной фазы, где
    оба соседа буферизуются на свой радиус). Инфляция и число сегментов —
    те же, что уже применяются к этому классу вычитания в этом модуле (см.
    `_CLEARANCE_INFLATION`).
    """
    if not items:
        return area
    return area.difference(
        unary_union(
            [
                item.geometry.buffer(clearance_m * _CLEARANCE_INFLATION, quad_segs=_CLEARANCE_QUAD_SEGS)
                for item in items
            ]
        )
    )


def _limit_by_density(
    items: list[PlantingItem], area_m2: float, density_per_ha: float | None
) -> list[PlantingItem]:
    """Оставить лучшие по оценке, если задана плотность посадки.

    Существует потому, что «сколько влезает» и «сколько нужно» — разные
    величины, и до сих пор алгоритм считал первую. `greedy_select` принимает
    каждого кандидата, который не конфликтует с уже принятыми, то есть
    заполняет каждый легально доступный квадратный метр. Проектировщик так не
    делает: он выбирает места и намеренно оставляет пустое. Без ограничения
    план на реальной улице выходил лесом из 3892 посадок на 4,6 га, где ни
    ряды, ни куртины не читаются — их забивает сплошная масса.

    Нормативной величины плотности в доступных актах нет (515-ПП задаёт
    штуки на квадратный метр ВНУТРИ группы, а не по территории) — значение
    по умолчанию (`DEFAULT_DENSITY_PER_HA`) поэтому практика, не норма, и
    помечено как таковое там же, где определено; `0` или отрицательное
    значение по-прежнему явно отключает ограничение для конкретного типа.

    Отбор — по убыванию оценки, той же, по которой greedy_select уже
    расставлял приоритет: ограничение отсекает худшие места, а не случайные.
    """
    if density_per_ha is None or density_per_ha <= 0 or area_m2 <= 0:
        return items
    allowed = max(1, int(round(density_per_ha * area_m2 / 10_000)))
    if len(items) <= allowed:
        return items
    return sorted(items, key=lambda item: item.score, reverse=True)[:allowed]


def _limit_row_and_group_by_density(
    row_items: list[PlantingItem],
    group_items: list[PlantingItem],
    area_m2: float,
    density_per_ha: float | None,
) -> list[PlantingItem]:
    """Тот же лимит, что `_limit_by_density`, но ряд получает первое право на
    бюджет плотности, а куртина — только то, что от него осталось.

    Нужно потому, что у куртины физически больше кандидатов, чем у ряда:
    десятки куртин по IN_GROUP_PITCH_M дают тысячи точек, а ряд ограничен
    длиной ориентиров. Общий отбор по одной оценке (как в `_limit_by_density`)
    заполнил бы весь бюджет куртиной просто по численному перевесу, даже если
    оценка `score_fn` не отдаёт ей предпочтения намеренно — ровно это и
    произошло живьём на реальной улице (2. Песчаный переулок): 213 в ряду
    против 1046 куртиной при общем отборе, хотя настоящий проект на этой же
    улице даёт обратное соотношение — 1171 изгородью против 21 куртины.
    Куртина там — украшение по краям, а не основной объём, и бюджет должен
    отражать это, а не численную случайность двух пулов кандидатов.
    """
    if density_per_ha is None or density_per_ha <= 0 or area_m2 <= 0:
        return row_items + group_items
    allowed = max(1, int(round(density_per_ha * area_m2 / 10_000)))
    if len(row_items) >= allowed:
        return sorted(row_items, key=lambda item: item.score, reverse=True)[:allowed]
    budget_left = allowed - len(row_items)
    if len(group_items) <= budget_left:
        return row_items + group_items
    capped_groups = sorted(group_items, key=lambda item: item.score, reverse=True)[:budget_left]
    return row_items + capped_groups


def choose_species_palette(
    plan_key: str,
    planting_type: str,
    catalogue: SpeciesCatalogue | None = None,
    count: int = 3,
) -> list[Species]:
    """Несколько пород на один тип посадки — палитра плана.

    Одна порода на весь тип давала монотонность: план читался как равномерное
    заполнение, а не как замысел. Настоящий дендроплан устроен иначе — аллея
    одной породой (в ряду однородность и есть смысл), а группы разными.

    Порядок детерминирован от `plan_key`: первая порода палитры идёт в ряд,
    остальные — по куртинам. Тот же сид, что и у выбора одной породы, чтобы
    план оставался чистой функцией от рецепта.
    """
    catalogue = catalogue or load_catalogue()
    pool = catalogue.for_type(planting_type)
    if not pool:
        return []
    rng = random.Random(zlib.crc32(f"{plan_key}:{planting_type}:species".encode()))
    shuffled = list(pool)
    rng.shuffle(shuffled)
    return shuffled[: max(1, count)]


def choose_species(
    plan_key: str,
    planting_type: str,
    catalogue: SpeciesCatalogue | None = None,
) -> Species | None:
    """Порода для этого типа посадки в этом плане — одна на весь тип.

    Не по объекту, и это осознанно. Во-первых, `greedy_select` отбирает по
    одному радиусу кроны на тип (см. placement.py), так что разные породы в
    одной россыпи сделали бы требуемое расстояние между соседями
    неопределённым. Во-вторых, так и выглядит настоящий дендроплан: посадки
    идут группами одной породы, а не случайной смесью в одной точке.

    Сид детерминированный и отдельный от сида расстановки — пересчёт
    схлопнутого плана обязан выдать ту же породу, а не свежий случайный выбор.
    """
    catalogue = catalogue or load_catalogue()
    pool = catalogue.for_type(planting_type)
    if not pool:
        return None
    palette = choose_species_palette(plan_key, planting_type, catalogue, count=1)
    return palette[0] if palette else None


def norms_for_species(
    norms: PlantingNorms,
    planting_type: str,
    species: Species | None,
    catalogue: SpeciesCatalogue,
) -> PlantingNorms:
    """Нормы с интервалом, выведенным из класса кроны выбранной породы.

    МГСН 1.02-02, п. 4.2.9.2: 8-10 м широкая крона, 5-6 м средняя, 3-4 м узкая
    (берётся нижняя граница — это минимум, см. species.yaml).

    Радиус кроны для отбора берётся как половина интервала, а НЕ как половина
    реального диаметра кроны породы. Это разные вопросы: `min_distance_m`
    служит ещё и шагом сетки кандидатов, и пара «шаг сетки / радиус отбора»
    обязана держать отношение 2:1, иначе отбор перестаёт отсеивать соседей —
    именно так когда-то сломали плотность кустарника. Реальный диаметр кроны
    используется в другом месте: им масштабируются отступы по примечанию 1 к
    таблицам 743-ПП и СП.
    """
    if species is None or planting_type not in CROWN_SPACING_TYPES:
        return norms
    spacing = catalogue.spacing_for_crown(species.crown)
    if not spacing:
        return norms
    return norms.with_spacing_override(planting_type, spacing)


def _plan_type_items(
    plan_key: str,
    planting_type: str,
    utilities: list[Utility],
    zones: list[Zone],
    territory: BaseGeometry,
    score_fn: Callable,
    norms: PlantingNorms,
    keep_spacing_for: Collection[str],
    species_override: str | None,
    pattern: str,
    density_per_ha: float | None,
    catalogue: SpeciesCatalogue,
) -> list[PlantingItem]:
    """Полная генерация одного типа посадки: палитра пород -> ряд вдоль
    ориентиров -> куртины -> россыпь в остатке -> ограничение по плотности.

    Module-level, а не вложенная в plan_items, ровно потому, что это делает
    функцию picklable — plan_items может отдать её в отдельный процесс
    (ProcessPoolExecutor не умеет пиклить локальные функции/замыкания), в
    отличие от `score_fn`, приходящего аргументом: реальные вызывающие
    (pipeline_service.py, scripts/plan_dxf.py) передают `scorer.as_score_fn()`
    — bound method модульного класса (HeuristicScorer/MLScorer), которая сама
    пиклится вместе со своим `self` (нормы, existing_greenery, построенный
    STRtree-индекс, для MLScorer — joblib-загруженный sklearn pipeline — всё
    это уже проверено picklable). Ряд/куртины (`patterns.py`) добавляют сюда
    только `RowGuide` (frozen dataclass с shapely-геометрией) и
    `PlantingCandidate` — оба уже пиклятся тем же путём, что `Utility`/`Zone`.
    """
    override = species_override
    palette = choose_species_palette(plan_key, planting_type, catalogue)
    # Породу ряда берём первой из палитры: в ряду однородность и есть
    # смысл, а остальные породы уходят по куртинам.
    species = catalogue.get(override) if override else (palette[0] if palette else None)
    type_norms = norms
    if planting_type not in keep_spacing_for:
        type_norms = norms_for_species(norms, planting_type, species, catalogue)

    exclusion = build_exclusion_zone(
        utilities,
        zones,
        planting_type,
        type_norms,
        species,
        catalogue.crown_reference_diameter_m,
    )
    margin = type_norms.territory_margin_for(planting_type)
    buildable = buildable_area(territory, exclusion, zones, territory_margin_m=margin)
    spacing = type_norms.spacing_for(planting_type)

    selected: list[PlantingItem] = []
    remaining = buildable
    row_items: list[PlantingItem] = []
    group_items: list[PlantingItem] = []

    wants_rows = pattern in ("auto", "row") and planting_type in ROW_PLANTING_TYPES
    if wants_rows:
        # Породу ряда выбирает не жребий, а площадка: у широкой кроны и
        # интервал вдвое больше, и отступы масштабируются примечанием 1 к
        # таблицам, так что на тесной улице она даёт единицы посадок.
        # Замерено: дуб (крона 10 м) дал 14 деревьев там, где узкая крона
        # даёт сотни. Проектировщик в таком месте берёт породу поуже —
        # здесь это делается перебором палитры по фактическому результату.
        species, type_norms, rows, guides = _fit_row_species(
            [species] if override else palette,
            planting_type,
            zones,
            territory,
            utilities,
            norms,
            keep_spacing_for,
            catalogue,
            row_pitch_override_m=IN_ROW_PITCH_M.get(planting_type),
        )
        spacing = type_norms.spacing_for(planting_type)
        exclusion = build_exclusion_zone(
            utilities, zones, planting_type, type_norms, species, catalogue.crown_reference_diameter_m
        )
        buildable = buildable_area(
            territory, exclusion, zones, territory_margin_m=type_norms.territory_margin_for(planting_type)
        )
        remaining = buildable
        row_items = greedy_select(rows, score_fn, type_norms)
        for item in row_items:
            if species is not None:
                item.species = species.name
            # Пометка идёт в rationale, а не в новое поле PlantingItem:
            # поле пришлось бы протаскивать через строки БД и миграцию ради
            # сведения, которое нужно отчёту и CLI, а не доменной модели.
            item.rationale = f"{ROW_RATIONALE_PREFIX} {item.rationale}"
        selected.extend(row_items)

    seed = zlib.crc32(f"{plan_key}:{planting_type}".encode())

    wants_groups = pattern in ("auto", "group") and planting_type in GROUP_PLANTING_TYPES
    if wants_groups and not remaining.is_empty:
        # Отбор внутри куртины идёт по ГРУППОВОМУ интервалу, а не по
        # обычному: иначе greedy_select, буферизующий каждую точку на
        # canopy_radius_m от одиночной посадки, прорядил бы группу до той
        # же россыпи, ради ухода от которой она и делается.
        group_norms = type_norms.with_spacing_override(planting_type, IN_GROUP_PITCH_M)
        # Куртина не должна перекрыть только что поставленную изгородь — тот
        # же класс проблемы, что и у россыпи после ряда ниже (свой
        # пространственный индекс на каждую фазу, общей памяти нет).
        group_area = _subtract_item_footprints(remaining, row_items, spacing.min_distance_m)
        discs = group_positions(group_area, GROUP_PITCH_M, GROUP_RADIUS_M, seed)
        # Индексы отступов/зонирования — по одному на весь тип, а не на
        # куртину: ни `exclusion`, ни `zones` не меняются между куртинами
        # одного типа, а fill_group's собственная перестройка индексов без
        # них платит заново на каждую куртину (замерено на «1. Олимпийская
        # деревня»: ExclusionIndex без сегментного индекса — ~5с/вызов,
        # ZoningIndex — ~28мс/вызов, оба × 235 куртин — см. fill_group).
        exclusion_index = ExclusionIndex(exclusion)
        zoning_index = ZoningIndex(zones)
        # Каждая куртина обрабатывается отдельно и получает СВОЮ породу.
        # Одновидовая группа — это как устроен настоящий дендроплан, и
        # именно смена породы между куртинами делает план рисунком, а не
        # равномерным заполнением. Раздельная обработка возможна потому,
        # что куртины по построению не соприкасаются (радиус 3 м при шаге
        # центров 14 м), так что общий отбор между ними не нужен.
        for index, disc in enumerate(discs):
            disc_species = species if override else _palette_pick(palette, index)
            clumps = fill_group(
                disc,
                exclusion,
                planting_type,
                IN_GROUP_PITCH_M,
                seed + index,
                zoning_zones=zones,
                exclusion_index=exclusion_index,
                zoning_index=zoning_index,
            )
            if not clumps:
                continue
            for item in greedy_select(clumps, score_fn, group_norms):
                item.rationale = f"{GROUP_RATIONALE_PREFIX} {item.rationale}"
                if disc_species is not None:
                    item.species = disc_species.name
                group_items.append(item)

    # Тип, посаженный куртинами, россыпью НЕ досыпается. Две причины, и обе
    # существенные. Композиционная: смысл куртины в том, что между группами
    # пусто, а досыпанная поверх россыпь возвращает ровно тот равномерный
    # крап, ради ухода от которого группы и делались. Вычислительная:
    # вычитание крон нескольких тысяч посадок из допустимой площади, которая
    # на реальной улице состоит из тысячи с лишним кусков, стоило минут —
    # замерено, весь прогон уходил за 23 минуты при 51 секунде на сами
    # куртины.
    #
    # Ряд и куртина делят один бюджет плотности, но не поровну — ряд получает
    # приоритет (`_limit_row_and_group_by_density`, см. её docstring), потому
    # что у куртины физически больше кандидатов и общий отбор по одной оценке
    # отдал бы ей весь бюджет по численному перевесу, а не по замыслу.
    if wants_groups and (row_items or group_items):
        return _limit_row_and_group_by_density(row_items, group_items, territory.area, density_per_ha)

    if pattern not in ("row", "group") and not remaining.is_empty:
        # Россыпь идёт ВТОРОЙ породой палитры, если она есть: аллея одной
        # породы и свободные группы другой — это и отличает план-рисунок
        # от равномерной раскладки. Интервал берётся под эту породу, её
        # класс кроны может отличаться от рядовой.
        # Первая порода палитры, ОТЛИЧНАЯ от рядовой, а не просто вторая
        # по счёту: породу ряда выбирает площадка (_fit_row_species), и она
        # запросто оказывается той же, что стоит в палитре второй. Именно
        # так и вышло — весь древесный ярус получился одновидовым при
        # палитре из трёх. Аллея одной породы и свободные группы другой —
        # это и отличает план-рисунок от равномерной раскладки.
        loose_species = species if override else _other_species(palette, species)
        loose_norms = type_norms
        loose_exclusion = exclusion
        loose_area = remaining
        if loose_species is not None and (loose_species is not species):
            # Зона отступов и допустимая площадь пересчитываются ПОД ЭТУ
            # породу. Без этого россыпь сажает по чужим отступам: площадь
            # посчитана под рядовую породу, а сажается другая, с иным
            # классом кроны и, значит, иными отступами (примечание 1 к
            # таблицам 743-ПП и СП масштабирует их по диаметру кроны).
            # Живьём это дало 52 нарушения из 1262 — план, который
            # отвергает собственная проверка сервиса, ровно тот же класс
            # ошибки, что уже ловили на буферах.
            if planting_type not in keep_spacing_for:
                loose_norms = norms_for_species(norms, planting_type, loose_species, catalogue)
            loose_exclusion = build_exclusion_zone(
                utilities, zones, planting_type, loose_norms, loose_species,
                catalogue.crown_reference_diameter_m,
            )
            loose_buildable = buildable_area(
                territory, loose_exclusion, zones,
                territory_margin_m=loose_norms.territory_margin_for(planting_type),
            )
            # Пересечение, а не замена: из `remaining` уже вычтены кроны
            # поставленного ряда, и терять это нельзя.
            loose_area = loose_buildable.intersection(remaining)
        if loose_area.is_empty:
            loose_area = remaining
            loose_exclusion = exclusion
            loose_norms = type_norms
            loose_species = species
        if row_items:
            # Из площади под россыпь вычитаются места уже поставленного
            # ряда: у каждого прохода greedy_select свой пространственный
            # индекс, общей памяти между фазами нет, и без вычитания
            # россыпь насыпала бы точки поверх аллеи.
            #
            # Радиус вычитания — ПОЛНЫЙ требуемый интервал, и берётся он по
            # большей из двух пород, а не по рядовой. Стояло
            # `spacing.canopy_radius_m` — радиус кроны рядовой породы, то
            # есть половина её же интервала, — и это ошибка сразу дважды.
            # Во-первых, вычитать надо целое расстояние между стволами, а
            # не половину: точка россыпи ставится в САМ остаток, её
            # собственный радиус здесь ничем не компенсируется. Во-вторых,
            # у россыпи своя порода и свой класс кроны: МГСН 1.02-02
            # п. 4.2.9.2 требует 8-10 м для широкой кроны против 3-4 для
            # узкой, так что мерить чужим интервалом нельзя. Для смешанной
            # пары акт числа не даёт — берётся большее из двух, по тому же
            # правилу «значение, при котором нарушить норму нельзя», по
            # которому во всём проекте выбирается число из диапазона.
            #
            # Замерено живьём на эталонной улице: берёза (интервал 5 м) в
            # ряду и дуб (8 м) россыпью вставали в 2,60 м друг от друга —
            # ровно радиус кроны берёзы, — и ни один отчёт этого не видел,
            # потому что проверка соответствия мерила только отступы от
            # сетей и зон, но не расстояние между самими посадками.
            clearance = max(
                spacing.min_distance_m,
                loose_norms.spacing_for(planting_type).min_distance_m,
            )
            loose_area = _subtract_item_footprints(loose_area, row_items, clearance)
        scattered = generate_candidates(
            loose_area, loose_exclusion, planting_type, loose_norms, zoning_zones=zones, seed=seed
        )
        for item in greedy_select(scattered, score_fn, loose_norms):
            if loose_species is not None:
                item.species = loose_species.name
            selected.append(item)

    return _limit_by_density(selected, territory.area, density_per_ha)


def plan_items(
    plan_key: str,
    utilities: list[Utility],
    zones: list[Zone],
    territory: BaseGeometry,
    planting_types: list[str],
    score_fn: Callable,
    norms: PlantingNorms,
    keep_spacing_for: Collection[str] = (),
    species_overrides: dict[str, str] | None = None,
    pattern: str = "auto",
    density_per_ha: dict[str, float] | None = None,
    catalogue: SpeciesCatalogue | None = None,
) -> list[PlantingItem]:
    """Сгенерировать посадки для одного плана.

    Каждый тип посадки генерируется независимо от одной и той же buildable-area,
    без взаимного вычитания — дерево или куст, попавший на площадь газона, это
    норма, а не баг: в живом озеленении дерево почти всегда стоит именно в
    газоне. Подрезка газона под уже стоящие посадки, если понадобится, —
    отдельная правка по воле пользователя, а не поведение генерации.

    Расстановка точек (дерево/куст) случайная, не сеточная (см. candidates.py),
    сид выводится из `plan_key` + типа через zlib.crc32 — устойчиво между
    процессами и запусками, в отличие от питоновского hash(). Именно это делает
    функцию чистой от рецепта плана: пересчёт схлопнутого плана обязан вернуть
    ту же раскладку, а не свежую случайную.

    `keep_spacing_for` — типы, у которых интервал задан пользователем явно и не
    должен подменяться выведенным из класса кроны. Без этого параметра
    пользовательская настройка молча терялась бы: и она, и породное правило
    приходят в одну и ту же `min_distance_m`, и снаружи их уже не различить.

    `pattern` — схема расстановки: "auto" (ряд вдоль линейных ориентиров плюс
    россыпь в остатке), "scatter" (только россыпь, прежнее поведение) или "row"
    (только ряды). Россыпь сама по себе нормативам не противоречит, но
    настоящим проектом не является: получается блу-нойз, который просто нигде
    не нарушает отступы. Сами акты описывают именно типы посадки — однорядную,
    двухрядную, групповую (743-ПП табл. 3.6.2; МГСН 1.02-02 п. 4.2.9.2), см.
    geo_engine/patterns.py.

    `species_overrides` — явно выбранная пользователем порода по типу посадки.
    Без неё порода выводится из `plan_key` детерминированно, но произвольно: от
    класса кроны зависит интервал, поэтому смена породы заметно меняет число
    посадок, и оставлять этот выбор только за сидом было бы неудобно.

    `density_per_ha` — плотность посадки, шт/га, по типу; типы, для которых
    вызывающий не передал значение, получают `DEFAULT_DENSITY_PER_HA` (см. её
    докстринг: практика, не норма, но уже проверенная на эталонной улице
    цифра, а не новая догадка) — `0` для типа явно отключает ограничение
    именно для него. Без ограничения `greedy_select` заполняет каждое
    легально доступное место, что на реальном масштабе даёт одновременно
    нереалистично плотный план и основную вычислительную нагрузку (куртины
    кустарника — см. CLAUDE.md).

    Типы посадок обрабатываются независимо друг от друга — своя палитра, свой
    ряд, свои куртины, своя россыпь, ноль пересечений между типами (см. выше)
    — поэтому при более чем одном типе и территории реального масштаба они
    считаются параллельно, по одному процессу на тип. На синтетике/в тестах
    (`_PARALLEL_PLAN_THRESHOLD`) остаётся последовательный путь: и накладные
    расходы на процесс (свой импорт shapely/pydantic, пиклинг
    utilities/zones/territory через границу процесса) там дороже самой
    работы, и результат от этого не меняется — порядок позиций в списке
    определяется порядком `planting_types`, не тем, какой процесс завершился
    первым (futures собираются в порядке отправки).
    """
    catalogue = catalogue or load_catalogue()
    species_overrides = species_overrides or {}
    density_per_ha = {**DEFAULT_DENSITY_PER_HA, **(density_per_ha or {})}

    worker_count = min(len(planting_types), os.cpu_count() or 1)
    if worker_count > 1 and len(utilities) + len(zones) >= _PARALLEL_PLAN_THRESHOLD:
        items: list[PlantingItem] = []
        with ProcessPoolExecutor(max_workers=worker_count) as pool:
            futures = [
                pool.submit(
                    _plan_type_items,
                    plan_key,
                    planting_type,
                    utilities,
                    zones,
                    territory,
                    score_fn,
                    norms,
                    keep_spacing_for,
                    species_overrides.get(planting_type),
                    pattern,
                    density_per_ha.get(planting_type),
                    catalogue,
                )
                for planting_type in planting_types
            ]
            for future in futures:
                items.extend(future.result())
        return items

    items = []
    for planting_type in planting_types:
        items.extend(
            _plan_type_items(
                plan_key,
                planting_type,
                utilities,
                zones,
                territory,
                score_fn,
                norms,
                keep_spacing_for,
                species_overrides.get(planting_type),
                pattern,
                density_per_ha.get(planting_type),
                catalogue,
            )
        )
    return items
