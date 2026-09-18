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

import math
import random
from dataclasses import dataclass

import shapely

from shapely.geometry import LineString, MultiLineString, Point
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from geo_engine.candidates import ZoningIndex, _clearance
from geo_engine.io.geometry_cleanup import merge_dashed_lines
from geo_engine.model import PlantingCandidate, PlantingType, Zone
from geo_engine.norms import PlantingNorms
from geo_engine.species import Species

# Вдоль чего сажают ряд. Здания и инженерные сети сюда намеренно не входят —
# см. docstring модуля.
ROW_GUIDE_ZONE_TYPES = ("road", "sidewalk", "tram_track")

# Типы посадки, для которых рядовая схема осмысленна. Газон — площадной
# объект, ряда из него не бывает.
ROW_PLANTING_TYPES = ("tree",)

# Типы, которые сажают куртинами. Это и есть тот паттерн, под который писаны
# плотные нормативные интервалы кустарника: 743-ПП табл. 3.6.2 даёт «групповая
# посадка кустарников — 0,3 м», а 515-ПП табл. 5 задаёт плотность в штуках на
# квадратный метр по видам (1-3 шт/м²). Разбрасывать кусты с таким интервалом
# по всей площади нельзя — получится сплошной ковёр, который в этом проекте
# уже однажды чинили; внутри ограниченной куртины это ровно норма.
GROUP_PLANTING_TYPES = ("shrub",)

# Расстояние между центрами куртин. Не норматив: акты задают плотность ВНУТРИ
# группы, но не то, как часто группы стоят. Взято из практики — куртина каждые
# полтора десятка метров читается как отдельная масса, а не сливается с
# соседней в тот же ковёр.
GROUP_PITCH_M = 14.0

# Радиус куртины. Тоже практика: 515-ПП оперирует плотностью на квадратный
# метр, то есть предполагает группу площадью в единицы-десятки метров —
# круг радиусом 3 м даёт около 28 м², это 28-84 куста по её плотности.
GROUP_RADIUS_M = 3.0

# Во сколько раз больше точек кидается внутрь куртины, чем поместилось бы по
# шагу. Прореживание делает greedy_select; двукратного запаса ему хватает, а
# больший только раздувает список кандидатов.
_GROUP_OVERSAMPLE = 2
_MAX_SAMPLES_PER_GROUP = 400

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

# Шаг вдоль ряда при ДВУХРЯДНОЙ посадке. 743-ПП, п. 3.6.4, табл. 3.6.2
# «Ориентировочные расстояния между деревьями и кустарниками на магистралях»:
# «Газон с однорядной посадкой деревьев — 5-6; с двухрядной посадкой деревьев
# — 7-8». Столбец таблицы называется «Расстояние между деревьями и
# кустарниками», то есть 7-8 м — это шаг ВДОЛЬ ряда, а не промежуток между
# рядами; промежуток эта таблица для деревьев не задаёт вовсе (примечание про
# ширину полосы относится к кустарнику). Берётся нижняя граница диапазона —
# то же правило, что и для интервала по классу кроны: диапазон задаёт минимум,
# и 7 ему удовлетворяет.
#
# Почему шаг БОЛЬШЕ, чем у однорядной: рядов два, и суммарная плотность
# посадки на погонный метр улицы остаётся той же.
DOUBLE_ROW_PITCH_M = 7.0

# Минимальная длина полезного куска второй эквидистанты, при которой второй ряд
# вообще имеет смысл. Два шага — тот же порог, по которому уже выбирается
# эквидистанта первого ряда: вдоль более короткого куска «ряда» не получается,
# получается пара случайных точек.
_MIN_SECOND_ROW_PITCHES = 2

# Во сколько промежутков между рядами «дотягивается» второй ряд, решая, считать
# ли участок первого ряда двухрядным. Полтора — с запасом больше самого
# промежутка (иначе пограничные точки попадали бы то в один набор, то в другой
# из-за погрешности буферов) и заведомо меньше расстояния, на котором второй
# ряд уже никак не влияет на восприятие первого.
_SECOND_ROW_REACH = 1.1


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


def points_along(line: LineString, pitch_m: float, anchor_m: float | None = None) -> list[Point]:
    """Точки вдоль линии с шагом `pitch_m`.

    Для незамкнутой линии — отцентрованные по её длине. Центрирование, а не
    «от начала»: отрезок редко делится на шаг нацело, и при отсчёте от начала
    весь остаток копится на дальнем конце — ряд выглядит оборванным. Здесь
    остаток делится поровну между концами.

    Для замкнутой (а эквидистанта ориентира замкнута всегда — это граница
    буфера) центрировать нечего, и остаток нельзя оставить на стыке: там
    первая и последняя точки оказались бы ближе шага друг к другу, то есть с
    нарушением интервала между стволами. Поэтому остаток раскладывается
    поровну на все промежутки — шаг выходит чуть больше номинального и
    никогда меньше, а ряд замыкается без шва.
    """
    if pitch_m <= 0:
        return []
    length = line.length
    if length < pitch_m:
        # Короче одного шага — одно дерево посередине, если отрезок вообще
        # заметной длины, иначе ничего.
        return [line.interpolate(length / 2)] if length >= pitch_m / 2 else []
    count = int(length // pitch_m)
    if anchor_m is not None:
        # Привязка к заданной точке вместо центрирования. Нужна второму ряду
        # двухрядной посадки: чтобы его деревья гарантированно попадали в
        # просветы первого, шаг надо отсчитывать не от начала СВОЕЙ линии, а от
        # дерева соседнего ряда. Кольца двух рядов разной длины, и сдвиг фазы
        # «от начала» никакого шахматного порядка не давал — тест это и поймал.
        first = anchor_m - pitch_m * math.floor(anchor_m / pitch_m)
        positions = [first + index * pitch_m for index in range(int((length - first) // pitch_m) + 1)]
        return [line.interpolate(t) for t in positions if 0.0 <= t <= length]
    if line.is_closed:
        step = length / count
        return [line.interpolate(index * step) for index in range(count)]
    start = (length - count * pitch_m) / 2
    return [line.interpolate(start + index * pitch_m) for index in range(count + 1)]


def _row_path(
    guide: RowGuide,
    buildable_area: BaseGeometry,
    band_m: float,
    inset_m: float,
    pitch_m: float,
) -> tuple[BaseGeometry, float] | None:
    """Линия, вдоль которой пойдёт ряд, и её смещение от ориентира: эквидистанта ориентира на связывающем
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

    best: tuple[BaseGeometry, float] | None = None
    best_length = 0.0
    for offset in offsets:
        ring = guide.geometry.buffer(offset).boundary
        usable = ring.intersection(buildable_area)
        length = usable.length
        if length >= pitch_m * 2:
            # Первая же эквидистанта, на которой помещается настоящий ряд, —
            # она и самая близкая к ориентиру, то есть самая правильная.
            return ring, offset
        if length > best_length:
            best, best_length = (ring, offset), length
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
    row_gap_m: float | None = None,
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
    # Индекс строится один раз на вызов, а не на точку: `ZoningIndex` появился
    # в candidates.py именно затем, чтобы убрать перебор всех зон на каждого
    # кандидата, и рядовая посадка обязана пользоваться тем же.
    zoning_index = ZoningIndex(zoning_zones)
    if buildable_area is None or buildable_area.is_empty:
        return []

    band_m = ROW_SEARCH_BAND_M if band_m is None else band_m

    candidates: list[PlantingCandidate] = []
    def emit(line: LineString, step_m: float, anchor_m: float | None = None) -> list[Point]:
        placed: list[Point] = []
        for point in points_along(line, step_m, anchor_m):
            if not buildable_area.contains(point):
                continue
            candidates.append(
                PlantingCandidate(
                    geometry=point,
                    planting_type=planting_type,
                    clearance_m=_clearance(point, exclusion_zone),
                    zoning=zoning_index.category_at(point),
                )
            )
            placed.append(point)
        return placed

    for guide in guides:
        found = _row_path(guide, buildable_area, band_m, inset_m, pitch_m)
        if found is None:
            continue
        ring, offset = found

        # Второй ряд — 743-ПП табл. 3.6.2, «газон с двухрядной посадкой
        # деревьев». Ставится НЕ всегда, а только если для него реально есть
        # место: считаем вторую эквидистанту, отодвинутую на промежуток между
        # рядами, и смотрим, остаётся ли от неё внутри допустимой площади хотя
        # бы два шага. На тесной улице её не остаётся, и посадка сама собой
        # сводится к однорядной — навязывать второй ряд там, где его негде
        # разместить, значило бы выдавать нарушение за замысел.
        #
        # Промежуток между рядами таблица для деревьев не задаёт, поэтому его
        # связывает МГСН 1.02-02 п. 4.2.9.2 — расстояние между стволами по
        # классу кроны, то есть `row_gap_m`. Его же проверяет compliance, так
        # что генератор и проверка тут считают по одному правилу.
        second_lines: list[LineString] = []
        if row_gap_m and row_gap_m > 0:
            outer = guide.geometry.buffer(offset + row_gap_m).boundary.intersection(buildable_area)
            # Длина проверяется у КАЖДОГО куска отдельно, а не в сумме. Сумма
            # обманывает: десяток двухметровых обрывков даёт «достаточную»
            # длину, рядом с которой никакого второго ряда на самом деле нет,
            # а раздутый буфером обрывок помечает двухрядным полтора десятка
            # метров первого ряда — и тот разрежается на пустом месте.
            minimum = DOUBLE_ROW_PITCH_M * _MIN_SECOND_ROW_PITCHES
            second_lines = [line for line in _as_lines(outer) if line.length >= minimum]

        if not second_lines:
            for line in _as_lines(ring):
                emit(line, pitch_m)
            continue

        # Шаг вдоль ряда у двухрядной посадки больше (7-8 м против 5-6):
        # рядов два, и плотность на погонный метр улицы остаётся той же.
        # Если порода по классу кроны требует ещё большего — побеждает он.
        double_step = max(DOUBLE_ROW_PITCH_M, pitch_m)

        # Широкий шаг применяется ТОЛЬКО там, где второй ряд реально есть.
        # Первая версия переключала шаг на весь ориентир целиком, стоило
        # второму ряду поместиться хоть где-то, — и на длинной улице это
        # разрежало первый ряд и на тех участках, где второго ряда нет.
        # Видно было сразу: аллея перестала читаться сплошной линией, а замер
        # показал рост медианы шага с 5,3 до 7,0 м по ВСЕМУ ряду. Поэтому
        # эквидистанта первого ряда делится на два набора отрезков — вдоль
        # второго ряда и без него, — и каждый идёт со своим шагом.
        alongside = unary_union([line.buffer(row_gap_m * _SECOND_ROW_REACH) for line in second_lines])
        first_row: list[Point] = []
        for line in _as_lines(ring.intersection(alongside)):
            first_row.extend(emit(line, double_step))
        for line in _as_lines(ring.difference(alongside)):
            emit(line, pitch_m)

        # Второй ряд — в шахматном порядке: дерево второго ряда попадает в
        # просвет первого, и это читается двухрядной аллеей, а не решёткой.
        # Отсчёт ведётся ОТ дерева первого ряда, спроецированного на линию
        # второго, а не от начала самой линии: кольца двух рядов разной длины,
        # и «сдвиг фазы на полшага от своего начала» никакого шахматного
        # порядка не даёт — первая версия так и делала, и тест это поймал.
        for line in second_lines:
            if first_row:
                nearest = min(first_row, key=line.distance)
                emit(line, double_step, line.project(nearest) + double_step / 2)
            else:
                emit(line, double_step)

    return candidates


# Акценты — солитеры. Композиционный приём, а не норматив: ни один акт не
# говорит, где ставить одиночное дерево. Нормативы здесь работают ровно так же,
# как везде (отступы и интервал проверяются), но САМ выбор места — проектное
# решение, и в отчёте он так и подписан.
#
# Критерий места выведен из геометрии, а не придуман: солитер это дерево,
# вокруг которого есть свободное пространство, иначе оно читается частью массы.
# Поэтому берётся точка, максимально удалённая от краёв допустимой площади
# (`shapely.maximum_inscribed_circle`), и она годится, только если этот отступ
# не меньше половины нормативного интервала породы — то есть дерево стоит с
# полным интервалом до любой границы во все стороны, а не прижато к ней.
#
# Замерено на эталонной улице, и результат стоит знать: у широкой кроны
# (интервал 8 м, нужен свободный радиус 4 м) таких мест НОЛЬ — ни у липы, ни у
# клёна, ни у дуба, при 29-54 кусках допустимой площади. У берёзы 18, у рябины
# 44. То есть на плотной московской улице солитер из крупнокронной породы
# невозможен физически, и это ответ данных, а не ограничение алгоритма.
ACCENT_PLANTING_TYPES = ("tree",)

# Во сколько интервалов разносятся соседние акценты. Не норматив: смысл акцента
# в том, что он один, и два солитера в двадцати метрах друг от друга — это уже
# не акценты, а редкая россыпь. Три интервала на глаз читаются как отдельные
# события.
ACCENT_SPACING_INTERVALS = 3.0

# Мельче этого куска допустимой площади вписанную окружность даже не считаем —
# заведомо не поместится, а вызов стоит денег на тысяче кусков.
_ACCENT_MIN_PART_AREA_M2 = 4.0

# Точность поиска вписанной окружности. 0.2 м на порядок мельче любого
# интервала и на порядок дешевле, чем точный поиск.
_ACCENT_TOLERANCE_M = 0.2

# Сколько акцентов искать на одном связном куске допустимой площади. Предохрани-
# тель, а не замысел: каждый шаг стоит поиска вписанной окружности, и на
# гигантском открытом участке цикл иначе крутился бы, пока не измельчит его
# весь. Восьми хватает, чтобы большой двор получил несколько солитеров, а не
# один.
_ACCENT_MAX_PER_PART = 8


def accent_positions(
    buildable_area: BaseGeometry,
    interval_m: float,
    spacing_intervals: float = ACCENT_SPACING_INTERVALS,
) -> list[tuple[Point, float]]:
    """Места для солитеров: точки с наибольшим свободным пространством вокруг.

    Возвращает пары (точка, радиус свободного места), от самой просторной к
    самой тесной, уже проряженные так, чтобы акценты не вставали кучей.

    Почему не «углы участка», с чего начиналась эта задача: контур обмера
    состоит из сотен вершин, и у эталонной улицы 458 из 664 поворачивают круче
    70° — это шум съёмки, а не углы площадки. Упрощение контура до осмысленных
    углов пришлось бы настраивать порогом, который ни из чего не следует.
    Свободное место, наоборот, прямо отвечает на вопрос «поместится ли здесь
    дерево так, чтобы читаться отдельно», и порога не требует — его задаёт сам
    нормативный интервал породы.
    """
    if buildable_area is None or buildable_area.is_empty or interval_m <= 0:
        return []

    required_radius = interval_m / 2
    parts = (
        list(shapely.get_parts(buildable_area))
        if buildable_area.geom_type.startswith("Multi")
        else [buildable_area]
    )

    min_gap = interval_m * spacing_intervals

    found: list[tuple[Point, float]] = []
    for part in parts:
        # По НЕСКОЛЬКУ точек с куска, а не по одной. Вписанная окружность даёт
        # ровно одно самое просторное место, и на реальной улице этого хватает
        # (допустимая площадь там разбита на сотни кусков), а на большой
        # открытой площадке весь участок — один кусок, и акцент получался
        # ровно один на весь проект. Поэтому найденное место вычитается, и
        # поиск повторяется по остатку.
        remainder = part
        for _ in range(_ACCENT_MAX_PER_PART):
            if remainder.is_empty or remainder.area < _ACCENT_MIN_PART_AREA_M2:
                break
            spoke = shapely.maximum_inscribed_circle(remainder, tolerance=_ACCENT_TOLERANCE_M)
            # Возвращается отрезок «центр -> ближайшая точка границы», его
            # длина и есть радиус наибольшей вписанной окружности.
            radius = spoke.length
            if radius < required_radius:
                break
            point = Point(spoke.coords[0])
            found.append((point, radius))
            remainder = remainder.difference(point.buffer(min_gap))

    found.sort(key=lambda pair: -pair[1])

    kept: list[tuple[Point, float]] = []
    for point, radius in found:
        if all(point.distance(other) >= min_gap for other, _ in kept):
            kept.append((point, radius))
    return kept


def group_positions(
    buildable_area: BaseGeometry,
    group_pitch_m: float,
    group_radius_m: float,
    seed: int,
) -> list[BaseGeometry]:
    """Круги-куртины, разбросанные по допустимой площади.

    Центры берутся псевдослучайно с отбраковкой ближе `group_pitch_m` друг к
    другу — тот же принцип, что у dart-throwing в candidates.py, только на
    порядок разреженнее: здесь разбрасывается не растение, а место под группу.

    Сид обязателен и детерминирован по той же причине, что и у расстановки
    точек: план обязан быть воспроизводимым, иначе пересчёт схлопнутого плана
    вернёт другую композицию.
    """
    if buildable_area is None or buildable_area.is_empty or group_pitch_m <= 0:
        return []

    rng = random.Random(seed)
    minx, miny, maxx, maxy = buildable_area.bounds
    width, height = maxx - minx, maxy - miny
    if width <= 0 or height <= 0:
        return []

    # Сколько попыток: столько, сколько мест теоретически поместилось бы на
    # bounding box, с запасом на отбраковку.
    attempts = int((width / group_pitch_m + 1) * (height / group_pitch_m + 1) * 8) + 16
    attempts = min(attempts, 20000)

    # Подготовленная геометрия обязательна, а не «для скорости»: допустимая
    # площадь на реальной улице собрана из тысяч буферов, и обычный
    # `contains` на ней стоит миллисекунды. Двадцать тысяч попыток превращали
    # генерацию групп в десятки минут — замерено, прогон пришлось прервать.
    shapely.prepare(buildable_area)

    # Отбраковка близких центров — по ячейкам сетки со стороной в шаг групп:
    # сравнивать каждую новую точку со всеми принятыми это O(n²), а соседей у
    # неё в любом случае не больше, чем в девяти окрестных ячейках.
    cell = group_pitch_m
    buckets: dict[tuple[int, int], list[Point]] = {}
    centres: list[Point] = []
    for _ in range(attempts):
        point = Point(minx + rng.random() * width, miny + rng.random() * height)
        if not buildable_area.contains(point):
            continue
        key = (int(point.x // cell), int(point.y // cell))
        near = [
            other
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            for other in buckets.get((key[0] + dx, key[1] + dy), ())
        ]
        if any(point.distance(other) < group_pitch_m for other in near):
            continue
        buckets.setdefault(key, []).append(point)
        centres.append(point)

    discs = []
    for centre in centres:
        disc = centre.buffer(group_radius_m).intersection(buildable_area)
        if not disc.is_empty:
            discs.append(disc)
    return discs


def fill_group(
    disc: BaseGeometry,
    exclusion_zone: BaseGeometry,
    planting_type: PlantingType,
    in_group_pitch_m: float,
    seed: int,
    zoning_zones: list[Zone] | None = None,
) -> list[PlantingCandidate]:
    """Кандидаты внутри ОДНОЙ куртины.

    По одной группе за раз, а не все сразу: каждая куртина получает свою
    породу, и одновидовая группа — это как устроен настоящий дендроплан.
    Раздельная обработка возможна потому, что куртины по построению не
    соприкасаются, так что общий отбор между ними не нужен.

    Плотно внутри и пусто между группами — именно это соответствует
    нормативным интервалам кустарника (743-ПП табл. 3.6.2: «групповая посадка
    кустарников — 0,3 м»). Прежняя россыпь с интервалом 3 м была компромиссом
    ровно из-за отсутствия групп: применить 0,3 м ко всей площади означало
    ковёр, а 3 м по всей площади — равномерный крап, не похожий на проект.
    """
    zoning_zones = zoning_zones or []
    zoning_index = ZoningIndex(zoning_zones)
    if disc is None or disc.is_empty:
        return []

    minx, miny, maxx, maxy = disc.bounds
    width, height = maxx - minx, maxy - miny
    if width <= 0 or height <= 0:
        return []

    shapely.prepare(disc)
    rng = random.Random(seed ^ 0x5EED)
    target = int((width / in_group_pitch_m + 1) * (height / in_group_pitch_m + 1) * _GROUP_OVERSAMPLE)
    points: list[Point] = []
    for _ in range(min(target + 8, _MAX_SAMPLES_PER_GROUP)):
        point = Point(minx + rng.random() * width, miny + rng.random() * height)
        if disc.contains(point):
            points.append(point)
    if not points:
        return []

    # Расстояние до зоны отступов — одним векторизованным вызовом на все точки
    # группы. Зона отступов на реальной улице собрана из тысяч буферов, и
    # поштучный `distance` по десяткам тысяч точек уводил генерацию за десять
    # минут — замерено, прогон пришлось прервать.
    if exclusion_zone is None or exclusion_zone.is_empty:
        clearances = [float("inf")] * len(points)
    else:
        clearances = [float(d) for d in shapely.distance(points, exclusion_zone)]

    return [
        PlantingCandidate(
            geometry=point,
            planting_type=planting_type,
            clearance_m=clearance,
            zoning=zoning_index.category_at(point),
        )
        for point, clearance in zip(points, clearances)
    ]
