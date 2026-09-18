"""Per-planting normative justification: why this spot is allowed, under which act.

This exists because the brief makes traceability a hard requirement, not a nicety:
every proposed planting must carry a reference to the act and the specific
clause/table behind it, and a result that cannot be checked that way is
explicitly not accepted ("«чёрный ящик» без привязки к нормам не принимается").

Deliberately a separate pass over the *final* items rather than something threaded
through candidate generation. `buffers.build_exclusion_zone()` merges every
constraint into one geometry, which is the right shape for fast geometric
selection but destroys exactly what an explanation needs: which individual
constraint applied, how much clearance there actually is, and which act sets it.
Recomputing per-item distances afterwards costs one vectorized nearest-neighbour
query per object type and keeps the selection algorithm unchanged.

The split that matters, and the reason ML is confined to one side of it:
  * whether a spot is *allowed* -- deterministic, from setbacks, always citable;
  * which allowed spot is *preferable* -- ranking, no clause to cite, because no
    act says one legal position beats another.
Only the first half is produced here.
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, field
from pathlib import Path

from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from geo_engine.model import PlantingItem, Utility, Zone
from geo_engine.norms import PlantingNorms
from geo_engine.species import SpeciesCatalogue, load_catalogue

# Zone types that impose a setback the same way a utility does. Hard obstacles
# (buildings, roads) are already subtracted by buffers.buildable_area, but the
# *explanation* still has to name them: "5 m from the building wall per table 9.1"
# is the reason a tree sits where it does just as much as a gas main is.
SETBACK_ZONE_TYPES = ("building", "road")

# Distances are compared with a tolerance, for the same reason validate_plan uses
# one: candidate geometry comes from buffered/eroded shapes, so a planting that
# sits exactly on its limit lands a few nanometres either side of it.
_TOLERANCE_M = 1e-3


@dataclass
class ConstraintCheck:
    """One object type checked against one planting, with its citation."""

    object_type: str
    required_m: float
    actual_m: float
    satisfied: bool
    source_id: str
    species_rule: str
    act: str
    clause: str
    citation: str
    table_row: str
    verified: bool

    def describe(self) -> str:
        verdict = "соблюдён" if self.satisfied else "НАРУШЕН"
        return (
            f"{OBJECT_LABELS_RU.get(self.object_type, self.object_type)}: "
            f"требуется {self.required_m:.2f} м, фактически {self.actual_m:.2f} м — {verdict} "
            f"[{self.citation}]"
        )


@dataclass
class ComplianceRecord:
    """The full justification for one planting, ready to serialize."""

    index: int
    planting_type: str
    species: str
    x: float
    y: float
    score: float
    compliant: bool
    binding_constraint: str | None
    summary: str
    checks: list[ConstraintCheck] = field(default_factory=list)
    ranking_rationale: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


OBJECT_LABELS_RU = {
    "heat_network": "теплосеть",
    "water_pipe": "водопровод",
    "sewer": "канализация/водосток",
    "gas_pipe": "газопровод",
    "cable_line": "кабель силовой/связи",
    "power_line_corridor": "охранная зона ЛЭП",
    "building": "стена здания",
    "road": "край проезжей части",
    "sidewalk": "край тротуара",
    "lighting_pole": "опора освещения/контактной сети",
    "retaining_wall": "подпорная стенка",
    "tram_track": "трамвайное полотно",
    "neighbour_tree": "соседнее дерево",
}

# Расстояние между стволами — МГСН 1.02-02, п. 4.2.9.2 (8-10 м широкая крона,
# 5-6 средняя, 3-4 узкая). Источник был объявлен в planting_norms.yaml и не
# использовался ни одной строчкой кода: интервал применялся при ОТБОРЕ
# (planner.norms_for_species -> placement.greedy_select), но нигде не
# проверялся, а отбор идёт отдельным проходом на каждую фазу посадки, и у
# каждого прохода свой пространственный индекс. Значит соблюдение интервала
# между фазами не гарантировал никто — и это не теория: замерено, берёза из
# ряда и дуб из россыпи вставали в 2,60 м при требуемых 8, а отчёт показывал
# полное соответствие, потому что мерил только отступы от сетей и зон.
CROWN_SPACING_SOURCE_ID = "mgsn_1_02_02_4_2_9_2"
NEIGHBOUR_OBJECT_TYPE = "neighbour_tree"

# Интервал между посадками проверяется ТОЛЬКО у деревьев — по той же причине,
# по которой planner.CROWN_SPACING_TYPES выводит его тоже только для них.
# 743-ПП табл. 3.6.2 даёт кустарнику 0,3-1,0 м, но это расстояние ВНУТРИ
# куртины или ряда, а не минимум между любыми двумя кустами плана; сама
# таблица названа «ориентировочные расстояния». Проверять по ней россыпь
# значило бы объявить нарушением нормальную плотную группу.
SPACING_CHECKED_TYPES = ("tree",)

CROWN_LABELS_RU = {"wide": "широкая", "medium": "средняя", "narrow": "узкая"}

PLANTING_LABELS_RU = {"tree": "дерево", "shrub": "кустарник", "lawn": "газон"}

# Stand-in id for a constraint whose object type has no citation configured.
UNMAPPED_SOURCE_ID = "__unmapped__"


def _constraint_groups(utilities: list[Utility], zones: list[Zone]) -> dict[str, list[BaseGeometry]]:
    groups: dict[str, list[BaseGeometry]] = {}
    for utility in utilities:
        if utility.geometry is not None and not utility.geometry.is_empty:
            groups.setdefault(utility.object_type, []).append(utility.geometry)
    for zone in zones:
        if zone.zone_type in SETBACK_ZONE_TYPES and zone.geometry is not None and not zone.geometry.is_empty:
            groups.setdefault(zone.zone_type, []).append(zone.geometry)
    return {k: v for k, v in groups.items() if v}


def _crown_interval(item: PlantingItem, catalogue: SpeciesCatalogue) -> tuple[float | None, str]:
    """Требуемый интервал по классу кроны и словесный класс, для цитаты."""
    species = catalogue.get(item.species)
    if species is None:
        return None, ""
    return catalogue.spacing_for_crown(species.crown), species.crown


def _neighbour_checks(
    items: list[PlantingItem], norms: PlantingNorms, catalogue: SpeciesCatalogue
) -> dict[int, ConstraintCheck]:
    """Расстояние до ближайшей соседней посадки того же типа.

    Отдельно от отступов от сетей: там ограничение — чужой объект чертежа,
    здесь — другая посадка этого же плана, и требуемое значение зависит от
    ОБЕИХ пород сразу. Для смешанной пары (широкая крона рядом со средней)
    акт числа не даёт, берётся больший из двух интервалов — то же правило
    «значение, при котором нарушить норму нельзя», по которому во всём
    проекте выбирается число из нормативного диапазона.

    Один `query_nearest` с `exclusive=True` на тип посадки, а не цикл по
    объектам: на реальном масштабе плана это сотни тысяч точек.
    """
    by_type: dict[str, list[int]] = {}
    for index, item in enumerate(items):
        if item.planting_type in SPACING_CHECKED_TYPES and item.geometry is not None:
            by_type.setdefault(item.planting_type, []).append(index)

    source = norms.sources.get(CROWN_SPACING_SOURCE_ID)
    checks: dict[int, ConstraintCheck] = {}
    for positions in by_type.values():
        if len(positions) < 2:
            # Одна посадка своего типа — соседа нет, и мерить нечего. Не
            # «соблюдено», а отсутствие ограничения: добавить сюда проверку
            # с бесконечным расстоянием значило бы засорить отчёт строкой,
            # которая ничего не утверждает.
            continue
        geometries = [items[i].geometry for i in positions]
        tree = STRtree(geometries)
        pairs, measured = tree.query_nearest(
            geometries, exclusive=True, all_matches=False, return_distance=True
        )
        for offset, local in enumerate(pairs[0]):
            index = positions[int(local)]
            neighbour = positions[int(pairs[1][offset])]
            own_interval, own_crown = _crown_interval(items[index], catalogue)
            other_interval, other_crown = _crown_interval(items[neighbour], catalogue)
            known = [v for v in (own_interval, other_interval) if v]
            if not known:
                # Порода не в каталоге либо у её класса кроны интервал не
                # задан — цитировать нечего, и выдумывать число нельзя.
                continue
            required = max(known)
            actual = float(measured[offset])
            checks[index] = ConstraintCheck(
                object_type=NEIGHBOUR_OBJECT_TYPE,
                required_m=required,
                actual_m=actual,
                satisfied=actual + _TOLERANCE_M >= required,
                source_id=CROWN_SPACING_SOURCE_ID if source else UNMAPPED_SOURCE_ID,
                species_rule=(
                    f"крона {CROWN_LABELS_RU.get(own_crown, own_crown or '—')} "
                    f"и {CROWN_LABELS_RU.get(other_crown, other_crown or '—')} "
                    f"({items[neighbour].species}) — берётся больший интервал"
                ),
                act=source.act if source else "Норматив не сопоставлен",
                clause=source.clause if source else "—",
                citation=source.citation() if source else "источник не определён",
                table_row="8-10 м широкая крона, 5-6 средняя, 3-4 узкая",
                verified=source.verified if source else False,
            )
    return checks


def explain_items(
    items: list[PlantingItem],
    utilities: list[Utility],
    zones: list[Zone],
    norms: PlantingNorms,
    catalogue: SpeciesCatalogue | None = None,
) -> list[ComplianceRecord]:
    """Build a citable justification for every item.

    One `STRtree.query_nearest` per object type over the whole item list, not a
    loop per item: at real scale a plan holds hundreds of thousands of plantings
    and the drawing thousands of constraint geometries, and the per-item version
    of this is the shape of loop that already had to be rewritten twice in
    candidates.py and placement.py.
    """
    if not items:
        return []

    catalogue = catalogue or load_catalogue()
    groups = _constraint_groups(utilities, zones)
    neighbour_checks = _neighbour_checks(items, norms, catalogue)
    geometries = [item.geometry for item in items]

    # object_type -> distance from each item to the nearest object of that type.
    # query_nearest over an array returns a (2, n) array of (input index, tree
    # index) plus the distances, so the positions have to be mapped back rather
    # than assumed to come out in input order.
    distances: dict[str, list[float]] = {}
    for object_type, group in groups.items():
        tree = STRtree(group)
        pairs, measured = tree.query_nearest(geometries, all_matches=False, return_distance=True)
        per_item = [float("inf")] * len(geometries)
        for position, item_index in enumerate(pairs[0]):
            per_item[int(item_index)] = float(measured[position])
        distances[object_type] = per_item

    records: list[ComplianceRecord] = []
    for index, item in enumerate(items):
        checks: list[ConstraintCheck] = []
        species = catalogue.get(item.species)
        for object_type in sorted(groups):
            # Не norms.setback_for(): после сверки с московскими актами
            # требуемый отступ перестал быть одним числом из таблицы — поверх
            # неё ложатся поимённые правила по породе, и цитировать надо то
            # правило, которое реально определило расстояние.
            resolution = norms.resolve_setback(
                object_type,
                item.planting_type,
                species,
                catalogue.crown_reference_diameter_m,
            )
            actual = distances[object_type][index]
            source = norms.sources.get(resolution.source_id)
            if source is None:
                # setback_for fell back to its fail-large default: say so instead
                # of implying an act demanded it.
                source_id = UNMAPPED_SOURCE_ID
                source_act = "Норматив не сопоставлен"
                clause = "—"
                citation = "источник не определён — применён консервативный отступ по умолчанию"
                row = ""
                verified = False
            else:
                source_id = resolution.source_id
                row = resolution.row
                source_act, clause, citation, verified = (
                    source.act,
                    source.clause,
                    source.citation(),
                    source.verified,
                )
            checks.append(
                ConstraintCheck(
                    object_type=object_type,
                    required_m=resolution.required_m,
                    actual_m=actual,
                    satisfied=actual + _TOLERANCE_M >= resolution.required_m,
                    source_id=source_id,
                    species_rule=resolution.species_rule,
                    act=source_act,
                    clause=clause,
                    citation=citation,
                    table_row=row,
                    verified=verified,
                )
            )

        neighbour = neighbour_checks.get(index)
        if neighbour is not None:
            checks.append(neighbour)

        records.append(_record(index, item, checks))
    return records


def _record(index: int, item: PlantingItem, checks: list[ConstraintCheck]) -> ComplianceRecord:
    violations = [c for c in checks if not c.satisfied]
    compliant = not violations

    # The binding constraint is the one with the least slack: it is the reason
    # the planting could not move closer to anything, and therefore the clause
    # an expert would check first.
    relevant = [c for c in checks if c.required_m > 0]
    binding = min(relevant, key=lambda c: c.actual_m - c.required_m) if relevant else None

    label = PLANTING_LABELS_RU.get(item.planting_type, item.planting_type)
    if compliant:
        if binding is None:
            summary = f"{label.capitalize()}: нормируемых препятствий рядом нет."
        else:
            slack = binding.actual_m - binding.required_m
            summary = (
                f"{label.capitalize()} допустим: все нормативные отступы соблюдены. "
                f"Определяющее ограничение — {OBJECT_LABELS_RU.get(binding.object_type, binding.object_type)}, "
                f"требуется {binding.required_m:.2f} м, фактически {binding.actual_m:.2f} м "
                f"(запас {slack:.2f} м). Основание: {binding.citation}"
                + (f", строка таблицы: «{binding.table_row}»" if binding.table_row else "")
                + (f". Поправка по породе: {binding.species_rule}" if binding.species_rule else "")
                + "."
            )
    else:
        worst = min(violations, key=lambda c: c.actual_m - c.required_m)
        summary = (
            f"{label.capitalize()} НЕ допустим: нарушен отступ от "
            f"{OBJECT_LABELS_RU.get(worst.object_type, worst.object_type)} — "
            f"требуется {worst.required_m:.2f} м, фактически {worst.actual_m:.2f} м. "
            f"Основание: {worst.citation}"
            + (f", строка таблицы: «{worst.table_row}»" if worst.table_row else "")
            + (f". Поправка по породе: {worst.species_rule}" if worst.species_rule else "")
            + "."
        )

    centroid = item.geometry.centroid
    return ComplianceRecord(
        index=index,
        planting_type=item.planting_type,
        species=item.species,
        x=float(centroid.x),
        y=float(centroid.y),
        score=float(item.score),
        compliant=compliant,
        binding_constraint=binding.object_type if binding else None,
        summary=summary,
        checks=checks,
        ranking_rationale=item.rationale,
    )


def unverified_sources(records: list[ComplianceRecord]) -> dict[str, int]:
    """How many bindings rest on a citation nobody has checked against the act.

    Surfaced in the CLI summary and the report header on purpose: the honest
    failure mode of a traceability feature is a confident-looking citation that
    nobody verified.
    """
    counts: dict[str, int] = {}
    for record in records:
        binding = record.binding_constraint
        if binding is None:
            continue
        for check in record.checks:
            if check.object_type == binding and not check.verified:
                counts[check.citation] = counts.get(check.citation, 0) + 1
    return counts


def report_payload(records: list[ComplianceRecord], norms: PlantingNorms, **meta) -> dict:
    """The machine-readable justification file the brief asks to ship with the plan.

    Normalized rather than flat: act, clause, table row and verification status
    are identical for every planting that hits the same rule, so they live in a
    `sources` table and each check references it by id. Measured on a real street
    (1740 plantings, 8 constraint types each) the flat form ran 3.3 KB per
    planting — about a gigabyte at the scale this pipeline is built for — while
    carrying the same information.
    """
    used: set[str] = {check.source_id for record in records for check in record.checks}

    sources: dict[str, dict] = {}
    for source_id in sorted(used):
        if source_id == UNMAPPED_SOURCE_ID:
            sources[source_id] = {
                "act": "Норматив не сопоставлен",
                "clause": "—",
                "title": "Тип объекта не сопоставлен ни с одним пунктом; применён консервативный отступ по умолчанию",
                "verified": False,
            }
            continue
        source = norms.sources.get(source_id)
        if source is not None:
            sources[source_id] = source.model_dump()

    rows: dict[str, str] = {}
    for record in records:
        for check in record.checks:
            if check.table_row:
                rows.setdefault(f"{check.source_id}|{check.object_type}|{record.planting_type}", check.table_row)

    items = []
    for record in records:
        items.append(
            {
                "index": record.index,
                "planting_type": record.planting_type,
                "species": record.species,
                "x": round(record.x, 3),
                "y": round(record.y, 3),
                "score": round(record.score, 4),
                "compliant": record.compliant,
                "binding_constraint": record.binding_constraint,
                "summary": record.summary,
                "ranking_rationale": record.ranking_rationale,
                "checks": [
                    {
                        "object_type": check.object_type,
                        "required_m": check.required_m,
                        "actual_m": round(check.actual_m, 3),
                        "satisfied": check.satisfied,
                        "source": check.source_id,
                        **({"species_rule": check.species_rule} if check.species_rule else {}),
                    }
                    for check in record.checks
                ],
            }
        )

    violations = sum(1 for r in records if not r.compliant)
    return {
        **meta,
        "total_items": len(records),
        "compliant_items": len(records) - violations,
        "violations": violations,
        "unverified_citations": unverified_sources(records),
        "sources": sources,
        "table_rows": rows,
        "items": items,
    }


TRACE_CSV_COLUMNS = [
    "index",
    "planting_type",
    "species",
    "x",
    "y",
    "compliant",
    "object_type",
    "required_m",
    "actual_m",
    "satisfied",
    "is_binding",
    "act",
    "clause",
    "verified",
    "species_rule",
    "table_row",
]


def write_trace_csv(records: list[ComplianceRecord], path: str | Path) -> int:
    """One row per (planting, constraint) — the "посадка → норма → пункт" trace.

    A second, flat form of the same justification, because that is the shape an
    expert actually checks in: sort by `satisfied`, filter by `act`, and every
    decision is one row with the clause next to the measured distance. Also far
    smaller than the JSON at scale — the brief accepts either.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRACE_CSV_COLUMNS, delimiter=";")
        writer.writeheader()
        for record in records:
            for check in record.checks:
                writer.writerow(
                    {
                        "index": record.index,
                        "planting_type": record.planting_type,
                        "species": record.species,
                        "x": f"{record.x:.3f}",
                        "y": f"{record.y:.3f}",
                        "compliant": int(record.compliant),
                        "object_type": check.object_type,
                        "required_m": f"{check.required_m:.2f}",
                        "actual_m": f"{check.actual_m:.3f}",
                        "satisfied": int(check.satisfied),
                        "is_binding": int(check.object_type == record.binding_constraint),
                        "act": check.act,
                        "clause": check.clause,
                        "verified": int(check.verified),
                        "species_rule": check.species_rule,
                        "table_row": check.table_row,
                    }
                )
                written += 1
    return written
