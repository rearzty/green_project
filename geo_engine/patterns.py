"""Паттерны посадки: ряд вдоль линейного ориентира вместо случайной россыпи.

До этого генератор делал ровно одно — разбрасывал точки методом dart-throwing
по всей допустимой площади и прореживал их жадным отбором. Результат
нормативам не противоречит, но настоящим проектом озеленения не является:
получается блу-нойз, который просто нигде не нарушает отступы.

Сами нормативные акты описывают не «россыпь», а **типы посадки**:

  * 743-ПП, п. 3.6.4, таблица 3.6.2 «Ориентировочные расстояния между
    деревьями и кустарниками на магистралях» перечисляет однорядную посадку
    деревьев (5-6 м), двухрядную (7-8 м), групповую (5-7 м), однорядную
    посадку кустарников (0,5-1 м для высоких, 0,3-0,4 для средних и низких) и
    групповую посадку кустарников (0,3 м);
  * МГСН 1.02-02, п. 4.2.9.2 предписывает шумозащитные насаждения
    проектировать «в виде однорядных или многорядных рядовых посадок».

Этот модуль даёт первый из них — рядовую посадку вдоль линейного ориентира
(край проезжей части, тротуар, граница участка). Ориентир буферизуется на
требуемый отступ, и посадки ставятся ровно по краю получившейся зоны с шагом
по нормативу: именно так выглядит аллея вдоль улицы.

Чего модуль НЕ делает и почему:

  * не ставит ряд вдоль здания или инженерной сети. Формально это было бы
    допустимо — отступ соблюдён, — но ряд, повторяющий трассу газопровода,
    это не аллея, а линия вдоль трубы. Ориентиром берётся то, вдоль чего
    аллею действительно сажают;
  * не заменяет россыпь целиком. Там, где линейного ориентира рядом нет,
    свободная площадь по-прежнему заполняется россыпью — у настоящего проекта
    обычно и то и другое: аллея вдоль проезда плюс свободные группы во дворе.
"""

from __future__ import annotations

from dataclasses import dataclass

from shapely.geometry import LineString, MultiLineString, Point
from shapely.geometry.base import BaseGeometry

from geo_engine.candidates import _clearance, _zoning_at
from geo_engine.io.geometry_cleanup import merge_dashed_lines
from geo_engine.model import PlantingCandidate, PlantingType, Zone
from geo_engine.norms import PlantingNorms
from geo_engine.species import Species

# Вдоль чего сажают ряд. Здания и инженерные сети сюда намеренно не входят —
# см. docstring модуля.
ROW_GUIDE_ZONE_TYPES = ("road", "sidewalk", "tram_track")

# Типы посадки, для которых рядовая схема осмысленна. Газон — площадной объект,
# ряда из него не бывает; кустарник в рядах тоже сажают, но его нормативные
# интервалы (0,3-1,0 м) относятся к плотной рядовой/групповой посадке, которую
# генератор пока не моделирует — см. planner.CROWN_SPACING_TYPES.
ROW_PLANTING_TYPES = ("tree",)

# Насколько отойти внутрь от края допустимой площади. Ставить ровно на край
# нельзя: он получен буферами, а буфер — многоугольная аппроксимация круга, и
# точка на самой кромке оказывается то внутри, то снаружи из-за погрешности в
# доли миллиметра. 5 см заведомо больше этой погрешности и заведомо меньше
# любого норматива.
ROW_INSET_M = 0.05

# Насколько далеко от ориентира искать край допустимой площади, сверх
# требуемого от него отступа. Нужно потому, что расстояние до края определяет
# НЕ обязательно сам ориентир: на реальной площадке отступ от границы участка
# (5 м у дерева) часто строже отступа от проезжей части (2 м), и кромка
# плантабельной зоны проходит там, где её задаёт связывающее ограничение, а не
# там, где кончается буфер улицы. Первая версия искала ряд ровно на кромке
# буфера ориентира и на такой площадке не находила ничего.
ROW_SEARCH_BAND_M = 6.0

# Короче этого ориентир не считается улицей. Бортовой камень приезжает из
# чертежа тысячами отдельных отрезков (на эталонной улице — 6510 штук), и
# первая версия строила «ряд» вдоль каждого огрызка: буфер вокруг
# двухметрового отрезка — это почти круг, и вдоль него помещается ровно одна
# точка. Получалась та же россыпь, только дороже. Сшивка отрезков в связные
# линии обязательна, а после неё короткие остатки всё равно надо отбрасывать:
# аллеи вдоль трёхметрового бордюрного огрызка не бывает.
MIN_GUIDE_LENGTH_M = 15.0


@dataclass(frozen=True)
class RowGuide:
    """Линия, вдоль которой ставится ряд, и требуемый от неё отступ."""

    geometry: BaseGeometry
    object_type: str
    setback_m: float


def collect_row_guides(
    zones: list[Zone],
    planting_type: PlantingType,
    norms: PlantingNorms,
    species: Species | None = None,
    crown_reference_diameter_m: float = 5.0,
) -> list[RowGuide]:
    """Ориентиры для рядовой посадки с уже посчитанным отступом от каждого.

    Отступ берётся через `resolve_setback`, а не из таблицы напрямую: он
    зависит от породы (МГСН 1.02-02 п. 4.2.8, 743-ПП табл. 3.6.1 прим. 1 и 3),
    и ряд, поставленный по табличному значению, оказался бы нарушением для
    крупнокронной породы — ровно та рассогласованность генератора и проверки,
    которая уже случалась на буферах.
    """
    by_type: dict[str, list[BaseGeometry]] = {}
    for zone in zones:
        if zone.zone_type not in ROW_GUIDE_ZONE_TYPES:
            continue
        if zone.geometry is None or zone.geometry.is_empty:
            continue
        by_type.setdefault(zone.zone_type, []).append(zone.geometry)

    guides: list[RowGuide] = []
    for object_type, geometries in by_type.items():
        resolution = norms.resolve_setback(
            object_type, planting_type, species, crown_reference_diameter_m
        )
        for line in _merged_guides(geometries):
            guides.append(
                RowGuide(geometry=line, object_type=object_type, setback_m=resolution.required_m)
            )
    return guides


def _merged_guides(geometries: list[BaseGeometry]) -> list[BaseGeometry]:
    """Сшить обрывки в связные линии и отбросить слишком короткие.

    Без сшивки рядовая посадка вырождается обратно в россыпь: бортовой камень
    приезжает из чертежа тысячами отдельных отрезков, буфер вокруг каждого
    почти круглый, и вдоль такого «ориентира» помещается одна точка. Замерено
    на эталонной улице — 6510 отрезков бортового камня, и первая версия этого
    модуля не дала ни одного настоящего ряда.

    Площадные ориентиры (если дорога пришла полигоном) не сшиваются — у них
    берётся контур, он и так связен.
    """
    lines = [g for g in geometries if g.geom_type in ("LineString", "MultiLineString")]
    areas = [g for g in geometries if g.geom_type in ("Polygon", "MultiPolygon")]

    merged: list[BaseGeometry] = []
    if lines:
        merged.extend(merge_dashed_lines(lines))
    for area in areas:
        merged.append(area.boundary)

    return [g for g in merged if not g.is_empty and g.length >= MIN_GUIDE_LENGTH_M]


def territory_guide(
    territory: BaseGeometry,
    planting_type: PlantingType,
    norms: PlantingNorms,
) -> RowGuide | None:
    """Граница участка как ориентир — по ней аллею сажают не реже, чем по улице.

    Отступ здесь свой (`territory_margin_m`), геометрически это не раздувание
    препятствия, а сжатие самой площадки — см. buffers.buildable_area.
    """
    if territory is None or territory.is_empty:
        return None
    margin = norms.territory_margin_for(planting_type)
    return RowGuide(geometry=territory.boundary, object_type="territory", setback_m=margin)


def _as_lines(geometry: BaseGeometry | None) -> list[LineString]:
    if geometry is None or geometry.is_empty:
        return []
    if isinstance(geometry, LineString):
        return [geometry]
    if isinstance(geometry, MultiLineString):
        return [g for g in geometry.geoms if not g.is_empty]
    parts = getattr(geometry, "geoms", None)
    if parts is None:
        return []
    lines: list[LineString] = []
    for part in parts:
        lines.extend(_as_lines(part))
    return lines


def points_along(line: LineString, pitch_m: float) -> list[Point]:
    """Точки вдоль линии с шагом `pitch_m`, отцентрованные по её длине.

    Центрирование, а не «от начала»: отрезок редко делится на шаг нацело, и
    при отсчёте от начала весь остаток копится на дальнем конце — ряд
    выглядит оборванным. Здесь остаток делится поровну между концами.
    """
    if pitch_m <= 0:
        return []
    length = line.length
    if length < pitch_m:
        # Короче одного шага — одно дерево посередине, если отрезок вообще
        # заметной длины, иначе ничего.
        return [line.interpolate(length / 2)] if length >= pitch_m / 2 else []
    count = int(length // pitch_m)
    start = (length - count * pitch_m) / 2
    return [line.interpolate(start + index * pitch_m) for index in range(count + 1)]


def _row_path(
    guide: RowGuide,
    buildable_area: BaseGeometry,
    band_m: float,
    inset_m: float,
    pitch_m: float,
) -> BaseGeometry | None:
    """Линия, вдоль которой пойдёт ряд: эквидистанта ориентира на связывающем
    расстоянии.

    Почему не край допустимой площади напрямую. Край — это контур со всеми
    вырезами от буферов сетей и зданий, он извилист, и ряд по нему получается
    рваным: замерено на эталонной улице — медиана шага между соседями 12.8 м
    при нормативных 8, попаданий в норматив 14%. Эквидистанта самого ориентира
    гладкая и повторяет улицу, ряд по ней выходит настоящим рядом (те же
    замеры: медиана ровно 8.00 м, 62%).

    Почему нельзя просто взять эквидистанту на `setback_m`. Расстояние до
    ближайшего разрешённого места задаёт связывающее ограничение, а им не
    обязательно оказывается улица: на реальной площадке отступ от границы
    участка (5 м у дерева) часто строже отступа от проезжей части (2 м).
    Поэтому эквидистанта отодвигается наружу шагами, пока не окажется внутри
    допустимой площади достаточно длинным куском.
    """
    step = max(pitch_m / 8, 0.25)
    offsets: list[float] = []
    distance = guide.setback_m + inset_m
    limit = guide.setback_m + band_m
    while distance <= limit:
        offsets.append(distance)
        distance += step

    best: BaseGeometry | None = None
    best_length = 0.0
    for offset in offsets:
        usable = guide.geometry.buffer(offset).boundary.intersection(buildable_area)
        length = usable.length
        if length >= pitch_m * 2:
            # Первая же эквидистанта, на которой помещается настоящий ряд, —
            # она и самая близкая к ориентиру, то есть самая правильная.
            return usable
        if length > best_length:
            best, best_length = usable, length
    return best


def row_candidates(
    guides: list[RowGuide],
    buildable_area: BaseGeometry,
    exclusion_zone: BaseGeometry,
    planting_type: PlantingType,
    pitch_m: float,
    zoning_zones: list[Zone] | None = None,
    inset_m: float = ROW_INSET_M,
    band_m: float | None = None,
) -> list[PlantingCandidate]:
    """Кандидаты рядовой посадки вдоль каждого ориентира.

    Ряд идёт по краю ДОПУСТИМОЙ ПЛОЩАДИ вблизи ориентира, а не по кромке
    буфера самого ориентира. Разница существенная: расстояние до края задаёт
    связывающее ограничение, а им не обязательно оказывается улица. На
    реальной площадке отступ от границы участка (5 м у дерева) часто строже
    отступа от проезжей части (2 м) — тогда кромка буфера улицы лежит внутри
    запрещённой полосы, и первая версия этой функции не находила там ни одной
    точки. Край допустимой площади — это по построению геометрическое место
    точек на минимально разрешённом расстоянии от ВСЕГО сразу, так что ряд
    оказывается прижат к улице ровно настолько, насколько позволяет норматив,
    какой бы из них ни связывал.

    `band_m` ограничивает поиск полосой вдоль ориентира: без неё в ряд попал бы
    весь контур допустимой площади, включая противоположный край участка,
    который к этой улице отношения не имеет.
    """
    zoning_zones = zoning_zones or []
    if buildable_area is None or buildable_area.is_empty:
        return []

    band_m = ROW_SEARCH_BAND_M if band_m is None else band_m

    candidates: list[PlantingCandidate] = []
    for guide in guides:
        usable = _row_path(guide, buildable_area, band_m, inset_m, pitch_m)
        for line in _as_lines(usable):
            for point in points_along(line, pitch_m):
                if not buildable_area.contains(point):
                    continue
                candidates.append(
                    PlantingCandidate(
                        geometry=point,
                        planting_type=planting_type,
                        clearance_m=_clearance(point, exclusion_zone),
                        zoning=_zoning_at(point, zoning_zones),
                    )
                )
    return candidates
