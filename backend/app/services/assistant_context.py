"""Сводка плана для Юны: факты, на которые она опирается в ответах.

Без неё Юна могла только менять настройки генерации и на вопрос «почему так
мало деревьев?» отвечала бы общими словами — или выдумывала. Здесь собирается
то, что сервис уже посчитал сам: число посадок по типам, схемам и породам,
объяснение площади (какая доля участка осталась под посадку и что закрыло
остальное — geo_engine.area_budget) и, если на карте выделена одна посадка,
её обоснование со ссылкой на акт (geo_engine.compliance).

Факты собираются один раз в структуру PlanFacts, а не сразу в текст: ими
пользуются двое. Языковой модели они уходят текстом (render_plan_facts), а
встроенный разбор команд без ИИ (assistant_rules.py) отвечает по тем же
числам напрямую — из текста их пришлось бы выковыривать обратно.

Текст короткий намеренно: каждый запрос к модели оплачивается (или
лимитирован) по токенам, и план на десятки тысяч посадок целиком туда не
отправить. Только агрегаты — несколько сотен токенов на любой размер плана.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from backend.app.db.models import Plan, Project
from backend.app.services import compliance_service
from backend.app.services.pipeline_service import plan_norms
from geo_engine.compliance import NEIGHBOUR_OBJECT_TYPE, OBJECT_LABELS_RU
from geo_engine.planner import (
    GROUP_RATIONALE_PREFIX,
    IN_PAVING_RATIONALE_PREFIX,
    ROW_RATIONALE_PREFIX,
    SOLITAIRE_RATIONALE_PREFIX,
    TREE_GROUP_RATIONALE_PREFIX,
)

TYPE_LABELS = {"tree": "деревья", "shrub": "кустарник", "lawn": "газон"}

_SCHEMES = (
    ("рядом вдоль улиц и границы участка", ROW_RATIONALE_PREFIX),
    ("группами деревьев", TREE_GROUP_RATIONALE_PREFIX),
    ("солитерами", SOLITAIRE_RATIONALE_PREFIX),
    ("куртинами", GROUP_RATIONALE_PREFIX),
)

_MAX_SPECIES = 6
_MAX_BLOCKERS = 3
_MAX_CHECKS = 4

_LABELS = {**OBJECT_LABELS_RU, NEIGHBOUR_OBJECT_TYPE: "соседнее дерево", "territory": "граница участка"}


@dataclass
class TypeFacts:
    """Деревья или кустарник плана: сколько, какими схемами, каких пород."""

    planting_type: str
    count: int
    # Только ненулевые схемы, в порядке _SCHEMES.
    schemes: list[tuple[str, int]] = field(default_factory=list)
    # Не попавшие ни в одну схему (рядом, группой…) — «россыпью».
    loose: int = 0
    in_paving: int = 0
    # Все породы, самые частые первыми: Юне без ИИ нужно найти «липы» среди
    # всех, а не только среди шести, что попадают в текст для модели.
    species: list[tuple[str, int]] = field(default_factory=list)


@dataclass
class BlockerFacts:
    object_type: str
    label: str
    share: float  # доля участка, закрытая этим ограничением в одиночку


@dataclass
class BudgetFacts:
    """Сколько площади осталось под посадку этого типа и что закрыло остальное."""

    planting_type: str
    usable_m2: float
    territory_m2: float
    usable_share: float
    blockers: list[BlockerFacts] = field(default_factory=list)


@dataclass
class CheckFacts:
    object_type: str
    label: str
    required_m: float
    actual_m: float
    satisfied: bool
    act: str
    clause: str


@dataclass
class SelectedItemFacts:
    """Обоснование одной выделенной посадки (geo_engine.compliance)."""

    planting_type: str
    species: str
    compliant: bool
    summary: str
    ranking_rationale: str = ""
    # Ближайшие к пределу ограничения — по запасу до нормы, самые жёсткие первыми.
    checks: list[CheckFacts] = field(default_factory=list)


@dataclass
class PlanFacts:
    plan_id: str
    scoring_mode: str
    tree_rows: int
    trees_in_paving: bool
    materialized: bool
    item_count: int
    planting_types: list[str] = field(default_factory=list)
    tree_spacing_m: float | None = None
    shrub_spacing_m: float | None = None
    # По типам, самые многочисленные первыми; пусто у нематериализованного плана.
    counts: dict[str, int] = field(default_factory=dict)
    # Только типы, у которых в плане есть посадки (деревья, кустарник).
    types: dict[str, TypeFacts] = field(default_factory=dict)
    lawn_area_m2: float = 0.0
    budgets: list[BudgetFacts] = field(default_factory=list)
    selected: SelectedItemFacts | None = None

    def count(self, planting_type: str) -> int:
        return self.counts.get(planting_type, 0)

    def budget(self, planting_type: str) -> BudgetFacts | None:
        return next((b for b in self.budgets if b.planting_type == planting_type), None)

    def species(self, planting_type: str) -> list[tuple[str, int]]:
        facts = self.types.get(planting_type)
        return facts.species if facts else []


def _find_plan(project: Project, plan_id: str) -> Plan | None:
    return next((p for p in project.plans if p.id == plan_id), None)


def _type_facts(plan: Plan, planting_type: str) -> TypeFacts | None:
    items = [i for i in plan.items if i.planting_type == planting_type]
    if not items:
        return None
    schemes = []
    for label, prefix in _SCHEMES:
        count = sum(1 for i in items if prefix in i.rationale)
        if count:
            schemes.append((label, count))
    return TypeFacts(
        planting_type=planting_type,
        count=len(items),
        schemes=schemes,
        loose=len(items) - sum(n for _, n in schemes),
        in_paving=sum(1 for i in items if IN_PAVING_RATIONALE_PREFIX in i.rationale),
        species=Counter(i.species for i in items).most_common(),
    )


def _budget_facts(budget: dict) -> BudgetFacts:
    return BudgetFacts(
        planting_type=budget.get("planting_type"),
        usable_m2=budget["usable_m2"],
        territory_m2=budget["territory_m2"],
        usable_share=budget["usable_share"],
        blockers=[
            BlockerFacts(object_type=b.get("object_type", ""), label=b["label"], share=b["share"])
            for b in (budget.get("blockers") or [])
        ],
    )


def _selected_item(project: Project, plan: Plan, item_id: str) -> SelectedItemFacts | None:
    if not plan.materialized:
        return None
    records = compliance_service.explain_items_by_id(project, plan, [item_id], plan_norms(plan))
    if not records:
        return None
    _, record = records[0]
    checks = sorted(record.checks, key=lambda c: c.actual_m - c.required_m)[:_MAX_CHECKS]
    return SelectedItemFacts(
        planting_type=record.planting_type,
        species=record.species,
        compliant=record.compliant,
        summary=record.summary,
        ranking_rationale=record.ranking_rationale,
        checks=[
            CheckFacts(
                object_type=c.object_type,
                label=_LABELS.get(c.object_type, c.object_type),
                required_m=c.required_m,
                actual_m=c.actual_m,
                satisfied=c.satisfied,
                act=c.act,
                clause=c.clause,
            )
            for c in checks
        ],
    )


def build_plan_facts(project: Project, plan_id: str, selected_item_id: str | None = None) -> PlanFacts | None:
    plan = _find_plan(project, plan_id)
    if plan is None:
        return None
    facts = PlanFacts(
        plan_id=plan.id,
        scoring_mode=plan.scoring_mode,
        tree_rows=plan.tree_rows,
        trees_in_paving=plan.trees_in_paving,
        materialized=plan.materialized,
        item_count=plan.item_count,
        planting_types=list(plan.planting_types),
        tree_spacing_m=plan.tree_spacing_m,
        shrub_spacing_m=plan.shrub_spacing_m,
        budgets=[_budget_facts(b) for b in plan.area_budget or []],
    )
    if plan.materialized:
        facts.counts = dict(Counter(item.planting_type for item in plan.items).most_common())
        for planting_type in ("tree", "shrub"):
            type_facts = _type_facts(plan, planting_type)
            if type_facts is not None:
                facts.types[planting_type] = type_facts
        # Площадь — в метрической CRS проекта, в ней план и хранится.
        facts.lawn_area_m2 = sum(
            float(getattr(item.geometry, "area", 0.0) or 0.0) for item in plan.items if item.planting_type == "lawn"
        )
    if selected_item_id:
        facts.selected = _selected_item(project, plan, selected_item_id)
    return facts


def _render_summary(facts: PlanFacts) -> list[str]:
    lines = [f"Сводка текущего плана (режим {facts.scoring_mode}, рядов деревьев {facts.tree_rows}, "
             f"деревья в покрытии {'да' if facts.trees_in_paving else 'нет'}):"]
    if not facts.materialized:
        lines.append(f"- всего посадок: {facts.item_count}")
    else:
        lines.append("- всего посадок: " + (", ".join(
            f"{TYPE_LABELS.get(t, t)} {n}" for t, n in facts.counts.items()) or "0"))
        for planting_type in ("tree", "shrub"):
            type_facts = facts.types.get(planting_type)
            if type_facts is None:
                continue
            schemes = [f"{label} {n}" for label, n in type_facts.schemes]
            if type_facts.loose:
                schemes.append(f"россыпью {type_facts.loose}")
            if type_facts.in_paving:
                schemes.append(f"из них в покрытии тротуара {type_facts.in_paving}")
            lines.append(f"- {TYPE_LABELS[planting_type]}: " + ", ".join(schemes))
            lines.append("  породы: " + ", ".join(f"{name} {n}" for name, n in type_facts.species[:_MAX_SPECIES]))
    for budget in facts.budgets:
        label = TYPE_LABELS.get(budget.planting_type, budget.planting_type)
        blockers = ", ".join(f"{b.label} — {round(100 * b.share)} %" for b in budget.blockers[:_MAX_BLOCKERS])
        lines.append(
            f"- {label}: под посадку остаётся {budget.usable_m2:.0f} м² из {budget.territory_m2:.0f} "
            f"({100 * budget.usable_share:.1f} % участка); больше всего закрывают: {blockers or '—'}"
        )
    return lines


def _render_selected(selected: SelectedItemFacts) -> list[str]:
    lines = [
        f"Выделенная на карте посадка: {TYPE_LABELS.get(selected.planting_type, selected.planting_type)}, "
        f"{selected.species}, {'соответствует нормам' if selected.compliant else 'НАРУШАЕТ нормы'}.",
        f"- {selected.summary}",
    ]
    if selected.ranking_rationale:
        lines.append(f"- почему выбрано это место: {selected.ranking_rationale[:300]}")
    for check in selected.checks:
        lines.append(
            f"- {check.label}: требуется {check.required_m:.2f} м, фактически {check.actual_m:.2f} м "
            f"({check.act}, {check.clause})"
        )
    return lines


def render_plan_facts(facts: PlanFacts) -> str:
    lines = _render_summary(facts)
    if facts.selected is not None:
        lines += _render_selected(facts.selected)
    return "\n".join(lines)


def plan_context(project: Project, plan_id: str, selected_item_id: str | None = None) -> str | None:
    facts = build_plan_facts(project, plan_id, selected_item_id)
    return render_plan_facts(facts) if facts is not None else None
