"""«Исправить нарушение в один клик» (п. 1 редизайна, Fix.dc.html).

Для посадки с нарушением норматива — до трёх вариантов с последствиями:

* **move** — ближайшая допустимая точка. Перебор сетки вокруг посадки (шаг
  0,1 м, радиус 3 м), фильтр — ровно те же условия, по которым её проверяет
  `edit_service.validate_*`: вне зоны отступов для типа и породы
  (`exclusion_cache`), интервал до соседних деревьев по классу кроны, внутри
  участка (и, если получается, дальше отступа от его границы — как ставит
  сам генератор). Из подходящих берётся лучшая по «смещение + штраф за выход
  из ряда»: ряд — это замысел плана, и сдвиг вдоль него дешевле, чем в сторону.
* **retype** — заменить дерево кустом, если куст в этой точке проходит
  отступы (кусту норма мягче: СП 42.13330.2016 табл. 9.1).
* **delete** — убрать; для посадки из ряда сообщается, какой пропуск останется.

Проверка кандидата и проверка плана — один и тот же код зон (`exclusion_cache`),
а не его копия: иначе «исправленная» посадка снова оказалась бы в нарушении.
Применение пачкой (`apply_fixes`) — для «Исправить все так же»: у каждой
посадки свой лучший вариант выбранного вида, а нужные ей соседи — уже с
учётом сдвигов, сделанных в этой же пачке.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
import shapely
from shapely.geometry import LineString, Point

from backend.app.db.models import Plan, PlantingItemRow
from backend.app.services import compliance_service, exclusion_cache
from backend.app.services.edit_service import _catalogue, _crown_interval, _items_by_ids, chain_indices
from backend.app.services.geo_io import _to_wgs84, db_to_shape, from_wgs84, planting_items_to_geojson_dicts, shape_to_db
from backend.app.services.pipeline_service import plan_norms
from geo_engine.compliance import NEIGHBOUR_OBJECT_TYPE, SPACING_CHECKED_TYPES

FIX_KINDS = ("move", "retype", "delete")

# Поиск допустимой точки.
SEARCH_RADIUS_M = 3.0
SEARCH_STEP_M = 0.1
# Выход из ряда «стоит» этого смещения: сдвиг на 0,3 м вдоль ряда лучше сдвига
# на 0,1 м в сторону, но не лучше, чем на 1,5 м вдоль. Число — суждение, не норма.
OFF_ROW_PENALTY_M = 1.0
# Точка «остаётся в ряду», если лежит не дальше этого от линии по соседям.
ROW_TOLERANCE_M = 0.2
# Запас до границы зоны отступов у выбранной точки: касание границы — уже
# нарушение (`intersects`), а обратная перепроекция координат шумит на ~1e-9 м.
BOUNDARY_MARGIN_M = 0.01
# Сколько лучших кандидатов проверяем точным расстоянием до зоны (оно дорогое
# на составной геометрии — `shapely.distance` не использует индекс).
_VERIFY_TOP = 40
_SPACING_MARGIN_M = 0.005
# «Ряд» у планировщика — группа с таким признаком в id (patterns.row_candidates).
_ROW_MARK = "-row-"


@lru_cache(maxsize=4)
def _offsets(radius_m: float) -> tuple[np.ndarray, np.ndarray]:
    """Смещения сетки (N, 2) и их длины, по возрастанию длины; сама точка не входит."""
    steps = int(round(radius_m / SEARCH_STEP_M))
    axis = np.arange(-steps, steps + 1) * SEARCH_STEP_M
    gx, gy = np.meshgrid(axis, axis)
    distance = np.hypot(gx, gy)
    keep = (distance <= radius_m + 1e-9) & (distance > SEARCH_STEP_M / 2)
    offsets = np.column_stack([gx[keep], gy[keep]])
    lengths = distance[keep]
    order = np.argsort(lengths, kind="stable")
    return offsets[order], lengths[order]


@dataclass
class _Move:
    x: float
    y: float
    shift_m: float
    keeps_row: bool | None


class _Trees:
    """Деревья плана массивами: для интервала между стволами нужны координаты и
    интервал каждого. Позиции правятся по ходу пачки (`move`/`drop`), а не
    через пространственный индекс — он бы устаревал после каждого сдвига, а
    линейный проход по массиву координат на плане в сто тысяч деревьев — доли
    миллисекунды на запрос."""

    def __init__(self, plan: Plan) -> None:
        catalogue = _catalogue()
        rows = [r for r in plan.items if r.planting_type in SPACING_CHECKED_TYPES]
        self.index = {r.id: i for i, r in enumerate(rows)}
        points = [db_to_shape(r.geometry) for r in rows]
        self.xy = np.array([[p.x, p.y] for p in points], dtype=float).reshape(-1, 2)
        self.interval = np.array([_crown_interval(catalogue, r.species) or 0.0 for r in rows], dtype=float)
        self.alive = np.ones(len(rows), dtype=bool)

    def around(self, x: float, y: float, radius: float, exclude_id: str) -> tuple[np.ndarray, np.ndarray]:
        """Координаты и интервалы деревьев в `radius` м от точки, кроме самой посадки."""
        if len(self.xy) == 0:
            return np.empty((0, 2)), np.empty(0)
        near = (np.hypot(self.xy[:, 0] - x, self.xy[:, 1] - y) <= radius) & self.alive
        own = self.index.get(exclude_id)
        if own is not None:
            near[own] = False
        return self.xy[near], self.interval[near]

    def move(self, item_id: str, x: float, y: float) -> None:
        i = self.index.get(item_id)
        if i is not None:
            self.xy[i] = (x, y)

    def drop(self, item_id: str) -> None:
        i = self.index.get(item_id)
        if i is not None:
            self.alive[i] = False


class _Context:
    """Общее для одного вызова: ряды плана и массивы деревьев строятся один
    раз, а не на каждую посадку пачки."""

    def __init__(self, plan: Plan) -> None:
        self.plan = plan
        self.project = plan.project
        self.norms = plan_norms(plan)
        self.groups: dict[str, list[PlantingItemRow]] = defaultdict(list)
        for row in plan.items:
            if row.group_id and _ROW_MARK in row.group_id:
                self.groups[row.group_id].append(row)
        self.trees = _Trees(plan)

    def row_neighbours(self, row: PlantingItemRow) -> list[PlantingItemRow]:
        """Остальные посадки ряда в порядке обхода (пусто, если это не ряд)."""
        if not row.group_id or _ROW_MARK not in row.group_id:
            return []
        others = [m for m in self.groups.get(row.group_id, []) if m.id != row.id]
        if len(others) < 2:
            return []
        points = [db_to_shape(m.geometry) for m in others]
        return [others[i] for i in chain_indices(points)]


def _find_move(ctx: _Context, row: PlantingItemRow, origin: Point | None = None, radius_m: float = SEARCH_RADIUS_M) -> _Move | None:
    """Ближайшая допустимая точка вокруг `origin` (по умолчанию — вокруг самой
    посадки; Alt при перетаскивании ищет вокруг курсора)."""
    point = origin if origin is not None else db_to_shape(row.geometry)
    if point.geom_type != "Point":
        return None
    offsets, lengths = _offsets(radius_m)
    xy = offsets + np.array([point.x, point.y])
    candidates = shapely.points(xy)
    ok = np.ones(len(candidates), dtype=bool)

    # 1. Вне зоны отступов — те же буферы, что у проверки плана.
    def outside(zone):
        if zone is None or zone.is_empty:
            return np.ones(len(candidates), dtype=bool)
        return ~shapely.intersects(candidates, zone)

    ok &= exclusion_cache.with_exclusion_zone(
        ctx.project, row.planting_type, outside, species=row.species, trees_in_paving=ctx.plan.trees_in_paving
    )
    if not ok.any():
        return None

    # 2. Внутри участка — и, если выходит, не ближе отступа от его границы,
    # как ставит сам генератор (buffers.buildable_area).
    margin = ctx.norms.territory_margin_for(row.planting_type)

    def inside(territory):
        plain = shapely.contains(territory, candidates)
        if margin <= 0:
            return plain, plain
        inner = territory.buffer(-margin)
        return plain, (shapely.contains(inner, candidates) if not inner.is_empty else np.zeros(len(candidates), dtype=bool))

    plain, with_margin = exclusion_cache.with_territory(ctx.project, inside)
    ok &= with_margin if (ok & with_margin).any() else plain
    if not ok.any():
        return None

    # 3. Интервал до соседних деревьев (МГСН 1.02-02 п. 4.2.9.2): требуется
    # больший из интервалов двух пород — то же правило, что у проверки.
    if row.planting_type in SPACING_CHECKED_TYPES:
        own_interval = _crown_interval(_catalogue(), row.species) or 0.0
        neighbour_xy, neighbour_interval = ctx.trees.around(
            point.x, point.y, radius_m + max(own_interval, float(ctx.trees.interval.max(initial=0.0))), row.id
        )
        if len(neighbour_xy):
            required = np.maximum(own_interval, neighbour_interval)
            distance = np.hypot(xy[:, 0:1] - neighbour_xy[None, :, 0], xy[:, 1:2] - neighbour_xy[None, :, 1])
            ok &= (distance >= required[None, :] + _SPACING_MARGIN_M).all(axis=1)
    if not ok.any():
        return None

    # 4. Остаётся ли точка в ряду: линия по остальным посадкам ряда проходит
    # и через место этой, так что сдвиг вдоль неё не ломает рисунок ряда.
    neighbours = ctx.row_neighbours(row)
    keeps: np.ndarray | None = None
    if neighbours:
        line = LineString([db_to_shape(m.geometry) for m in neighbours])
        keeps = shapely.distance(candidates, line) <= ROW_TOLERANCE_M
    score = lengths + (0.0 if keeps is None else np.where(keeps, 0.0, OFF_ROW_PENALTY_M))

    found = np.flatnonzero(ok)
    found = found[np.argsort(score[found], kind="stable")][:_VERIFY_TOP]

    # 5. Точное расстояние до зоны у лучших: касание границы — уже нарушение.
    def verify(zone):
        if zone is None or zone.is_empty:
            return int(found[0])
        for index in found:
            if shapely.distance(candidates[index], zone) >= BOUNDARY_MARGIN_M:
                return int(index)
        return None

    chosen = exclusion_cache.with_exclusion_zone(
        ctx.project, row.planting_type, verify, species=row.species, trees_in_paving=ctx.plan.trees_in_paving
    )
    if chosen is None:
        return None
    return _Move(
        x=float(xy[chosen, 0]),
        y=float(xy[chosen, 1]),
        shift_m=float(lengths[chosen]),
        keeps_row=None if keeps is None else bool(keeps[chosen]),
    )


def _shrub_fits(ctx: _Context, row: PlantingItemRow) -> bool:
    """Куст в этой же точке проходит отступы (интервал между стволами кусту не
    задан: 743-ПП табл. 3.6.2 — расстояние внутри посадки, не минимум)."""
    if row.planting_type != "tree":
        return False
    point = db_to_shape(row.geometry)
    if point.geom_type != "Point":
        return False

    def fits(zone):
        return zone is None or zone.is_empty or not bool(shapely.intersects(point, zone))

    return bool(exclusion_cache.with_exclusion_zone(ctx.project, "shrub", fits, species=None))


def _gap_after_delete(ctx: _Context, row: PlantingItemRow) -> float | None:
    """Какой пропуск появится в ряду: расстояние между его соседями по цепочке.
    Крайняя посадка пропуска не оставляет, и посадка вне ряда — тоже."""
    if not row.group_id or _ROW_MARK not in row.group_id:
        return None
    members = ctx.groups.get(row.group_id, [])
    if len(members) < 3:
        return None
    points = [db_to_shape(m.geometry) for m in members]
    order = chain_indices(points)
    position = next((k for k, i in enumerate(order) if members[i].id == row.id), None)
    if position is None or position == 0 or position == len(order) - 1:
        return None
    before, after = points[order[position - 1]], points[order[position + 1]]
    return float(before.distance(after))


def _display_point(x: float, y: float, source_crs: str | None) -> dict:
    shown = _to_wgs84(Point(x, y), source_crs)
    return {"x": float(shown.x), "y": float(shown.y)}


def _hypothetical(row: PlantingItemRow, *, geometry=None, planting_type: str | None = None) -> PlantingItemRow:
    return PlantingItemRow(
        plan_id=row.plan_id,
        geometry=shape_to_db(geometry if geometry is not None else db_to_shape(row.geometry)),
        planting_type=planting_type or row.planting_type,
        species=row.species,
        score=row.score,
        rationale=row.rationale,
    )


def _violated_type(record) -> str | None:
    """Класс ограничения, которому посадка проигрывает больше всего (без
    интервала до соседа: для него «насколько» считается по соседним деревьям)."""
    failed = [c for c in record.checks if not c.satisfied and c.object_type != NEIGHBOUR_OBJECT_TYPE]
    if not failed:
        return None
    return min(failed, key=lambda c: c.actual_m - c.required_m).object_type


def item_fixes(plan: Plan, item_id: str, source_crs: str | None) -> list[dict]:
    """Варианты исправления для одной посадки (см. модуль). Порядок —
    приоритет: лучший первым; `recommended` стоит только когда есть из чего
    выбирать."""
    ctx = _Context(plan)
    row = next((r for r in plan.items if r.id == item_id), None)
    if row is None:
        return []
    move = _find_move(ctx, row)
    shrub = row.planting_type == "tree" and _shrub_fits(ctx, row)

    # Одно обоснование на всё: как нарушена посадка сейчас, что скажет норма
    # про куст в этой точке и на сколько будет отодвинута дальше от нарушенного.
    probes = [row]
    moved_row = _hypothetical(row, geometry=Point(move.x, move.y)) if move else None
    shrub_row = _hypothetical(row, planting_type="shrub") if shrub else None
    probes += [r for r in (moved_row, shrub_row) if r is not None]
    records = [record for _, record in compliance_service.explain_rows(ctx.project, probes, ctx.norms)]
    current = records[0]
    violated = _violated_type(current)
    moved_record = records[probes.index(moved_row)] if moved_row is not None else None
    shrub_record = records[probes.index(shrub_row)] if shrub_row is not None else None

    options: list[dict] = []
    if move is not None:
        clearance = None
        if moved_record is not None:
            checks = [c for c in moved_record.checks if c.object_type != NEIGHBOUR_OBJECT_TYPE]
            same = [c for c in checks if c.object_type == violated] if violated else []
            pool = same or checks
            if pool:
                clearance = min(pool, key=lambda c: c.actual_m - c.required_m).actual_m
        if violated is None and row.planting_type in SPACING_CHECKED_TYPES:
            # Нарушен интервал до соседа — «сколько до него» после сдвига.
            neighbours, _ = ctx.trees.around(move.x, move.y, SEARCH_RADIUS_M + 12.0, row.id)
            if len(neighbours):
                clearance = float(np.hypot(neighbours[:, 0] - move.x, neighbours[:, 1] - move.y).min())
        options.append(
            {
                "kind": "move",
                "to": _display_point(move.x, move.y, source_crs),
                "shift_m": round(move.shift_m, 2),
                "clearance_m": None if clearance is None else round(float(clearance), 2),
                "keeps_row": move.keeps_row,
            }
        )
    if shrub_record is not None:
        checks = [c for c in shrub_record.checks if c.object_type != NEIGHBOUR_OBJECT_TYPE]
        same = [c for c in checks if c.object_type == violated] if violated else []
        pool = same or checks
        if same:
            required = same[0].required_m
        elif pool:
            required = min(pool, key=lambda c: c.actual_m - c.required_m).required_m
        else:
            required = None
        options.append({"kind": "retype", "type": "shrub", "valid": True, "required_m": None if required is None else round(float(required), 2)})
    gap = _gap_after_delete(ctx, row)
    options.append({"kind": "delete", "gap_m": None if gap is None else round(gap, 2)})

    for option in options:
        option["recommended"] = False
    if len(options) > 1:
        options[0]["recommended"] = True
    return options


@dataclass
class FixOutcome:
    previous: list[dict]
    created: list[dict]
    skipped: list[str]


def apply_fixes(plan: Plan, ids: list[str], kind: str, source_crs: str | None) -> FixOutcome:
    """«Исправить все так же»: лучший вариант вида `kind` у каждой посадки из
    `ids`; тем, у кого такого нет (куст в этом месте не проходит, свободной
    точки рядом нет), правка не делается, и они возвращаются в `skipped`.

    Возвращает снимки прежних посадок (для отмены) и новых (сдвинутых и
    пересаженных — у них прежние id; удалённых среди них нет). Отмена на
    фронте — та же пара «удалить новые, вернуть прежние», что у перерасстановки
    ряда (`respace_items`): id совпадают, поэтому сначала убрать, потом вернуть."""
    if kind not in FIX_KINDS:
        raise ValueError(f"Неизвестный вид исправления: {kind}")
    rows = _items_by_ids(plan, ids)
    ctx = _Context(plan)

    planned: list[tuple[PlantingItemRow, object]] = []
    skipped: list[str] = []
    for row in rows:
        if kind == "move":
            move = _find_move(ctx, row)
            if move is None:
                skipped.append(row.id)
                continue
            # Следующая посадка пачки видит эту уже на новом месте.
            ctx.trees.move(row.id, move.x, move.y)
            planned.append((row, move))
        elif kind == "retype":
            if not _shrub_fits(ctx, row):
                skipped.append(row.id)
                continue
            ctx.trees.drop(row.id)
            planned.append((row, "shrub"))
        else:
            ctx.trees.drop(row.id)
            planned.append((row, None))

    touched = [row for row, _ in planned]
    previous = planting_items_to_geojson_dicts(touched, source_crs)
    changed: list[PlantingItemRow] = []
    removed: set[str] = set()
    for row, action in planned:
        if kind == "move":
            row.geometry = shape_to_db(Point(action.x, action.y))  # type: ignore[union-attr]
            row.is_manual_edit = True
            changed.append(row)
        elif kind == "retype":
            row.planting_type = "shrub"
            row.is_manual_edit = True
            changed.append(row)
        else:
            removed.add(row.id)
    if removed:
        plan.items = [item for item in plan.items if item.id not in removed]
        plan.item_count -= len(removed)
    if planned:
        plan.has_manual_edits = True
    return FixOutcome(previous=previous, created=planting_items_to_geojson_dicts(changed, source_crs), skipped=skipped)


# Alt при перетаскивании ищет вокруг курсора, а курсор бывает глубоко в
# запретной зоне (здание с отступом 5 м) — радиус больше, чем у «Как исправить».
NEAREST_ALLOWED_RADIUS_M = 8.0


def nearest_allowed(plan: Plan, item_id: str, x: float, y: float, source_crs: str | None) -> dict | None:
    """Ближайшая допустимая точка для посадки `item_id`, если её поставить у
    (x, y) — «Alt: к ближайшему допустимому месту» при перетаскивании. Те же
    условия, что у «Как исправить» (см. `_find_move`); `None` — рядом места нет.
    (x, y) — в координатах отображения, как и ответ."""
    ctx = _Context(plan)
    row = next((r for r in plan.items if r.id == item_id), None)
    if row is None or row.planting_type == "lawn":
        return None
    origin = from_wgs84(Point(x, y), source_crs)
    move = _find_move(ctx, row, origin=origin, radius_m=NEAREST_ALLOWED_RADIUS_M)
    if move is None:
        return None
    return {"to": _display_point(move.x, move.y, source_crs), "shift_m": round(move.shift_m, 2)}
