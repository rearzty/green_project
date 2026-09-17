"""Пайплайн размещения: территория и ограничения -> отобранные посадки.

Чистая геометрия и скоринг, без базы и веб-фреймворка — в этом и смысл. Раньше
этот цикл жил внутри `pipeline_service._compute_planting_rows`, то есть
единственным способом запустить ядро проекта было поднять PostGIS и FastAPI.
ТЗ просит воспроизводимый пайплайн DXF → DXF и прямо принимает CLI как способ
сдачи, поэтому алгоритм живёт здесь, а оба вызывающих — CLI и backend —
гоняют один и тот же код.
"""

from __future__ import annotations

import random
import zlib
from collections.abc import Callable, Collection

from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from geo_engine.buffers import build_exclusion_zone, buildable_area
from geo_engine.candidates import generate_candidates
from geo_engine.model import PlantingItem, Utility, Zone
from geo_engine.norms import PlantingNorms
from geo_engine.patterns import (
    GROUP_PLANTING_TYPES,
    ROW_PLANTING_TYPES,
    collect_row_guides,
    group_candidates,
    row_candidates,
    territory_guide,
)
from geo_engine.placement import greedy_select
from geo_engine.species import Species, SpeciesCatalogue, load_catalogue

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
    штуки на квадратный метр ВНУТРИ группы, а не по территории), поэтому это
    пользовательский параметр без значения по умолчанию, а не ещё одна
    несверенная константа.

    Отбор — по убыванию оценки, той же, по которой greedy_select уже
    расставлял приоритет: ограничение отсекает худшие места, а не случайные.
    """
    if density_per_ha is None or density_per_ha <= 0 or area_m2 <= 0:
        return items
    allowed = max(1, int(round(density_per_ha * area_m2 / 10_000)))
    if len(items) <= allowed:
        return items
    return sorted(items, key=lambda item: item.score, reverse=True)[:allowed]


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
    rng = random.Random(zlib.crc32(f"{plan_key}:{planting_type}:species".encode()))
    return rng.choice(pool)


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
    """
    catalogue = catalogue or load_catalogue()
    species_overrides = species_overrides or {}
    density_per_ha = density_per_ha or {}
    items: list[PlantingItem] = []

    for planting_type in planting_types:
        override = species_overrides.get(planting_type)
        species = catalogue.get(override) if override else choose_species(plan_key, planting_type, catalogue)
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

        wants_rows = pattern in ("auto", "row") and planting_type in ROW_PLANTING_TYPES
        if wants_rows:
            guides = collect_row_guides(
                zones, planting_type, type_norms, species, catalogue.crown_reference_diameter_m
            )
            boundary_guide = territory_guide(territory, planting_type, type_norms)
            if boundary_guide is not None:
                guides.append(boundary_guide)

            rows = row_candidates(
                guides, buildable, exclusion, planting_type, spacing.min_distance_m, zoning_zones=zones
            )
            row_items = greedy_select(rows, score_fn, type_norms)
            for item in row_items:
                # Пометка идёт в rationale, а не в новое поле PlantingItem:
                # поле пришлось бы протаскивать через строки БД и миграцию ради
                # сведения, которое нужно отчёту и CLI, а не доменной модели.
                item.rationale = f"{ROW_RATIONALE_PREFIX} {item.rationale}"
            selected.extend(row_items)
            # Из площади под россыпь вычитаются кроны уже поставленного ряда.
            # Без этого второй проход greedy_select ничего не знает о первом и
            # насыпал бы точки поверх аллеи: у каждого прохода свой
            # пространственный индекс, общей памяти между ними нет.
            if row_items:
                taken = unary_union(
                    [item.geometry.buffer(spacing.canopy_radius_m) for item in row_items]
                )
                remaining = buildable.difference(taken)

        seed = zlib.crc32(f"{plan_key}:{planting_type}".encode())

        wants_groups = pattern in ("auto", "group") and planting_type in GROUP_PLANTING_TYPES
        if wants_groups and not remaining.is_empty:
            clumps = group_candidates(
                remaining, exclusion, planting_type, IN_GROUP_PITCH_M, seed, zoning_zones=zones
            )
            # Отбор внутри куртины идёт по ГРУППОВОМУ интервалу, а не по
            # обычному: иначе greedy_select, буферизующий каждую точку на
            # canopy_radius_m от одиночной посадки, прорядил бы группу до той
            # же россыпи, ради ухода от которой она и делается.
            group_norms = type_norms.with_spacing_override(planting_type, IN_GROUP_PITCH_M)
            group_items = greedy_select(clumps, score_fn, group_norms)
            for item in group_items:
                item.rationale = f"{GROUP_RATIONALE_PREFIX} {item.rationale}"
            selected.extend(group_items)

        # Тип, посаженный куртинами, россыпью НЕ досыпается. Две причины, и обе
        # существенные. Композиционная: смысл куртины в том, что между группами
        # пусто, а досыпанная поверх россыпь возвращает ровно тот равномерный
        # крап, ради ухода от которого группы и делались. Вычислительная:
        # вычитание крон нескольких тысяч посадок из допустимой площади, которая
        # на реальной улице состоит из тысячи с лишним кусков, стоило минут —
        # замерено, весь прогон уходил за 23 минуты при 51 секунде на сами
        # куртины.
        if wants_groups and selected:
            selected = _limit_by_density(selected, territory.area, density_per_ha.get(planting_type))
            if species is not None:
                for item in selected:
                    item.species = species.name
            items.extend(selected)
            continue

        if pattern not in ("row", "group") and not remaining.is_empty:
            scattered = generate_candidates(
                remaining, exclusion, planting_type, type_norms, zoning_zones=zones, seed=seed
            )
            selected.extend(greedy_select(scattered, score_fn, type_norms))

        selected = _limit_by_density(selected, territory.area, density_per_ha.get(planting_type))

        if species is not None:
            for item in selected:
                item.species = species.name

        items.extend(selected)
    return items
