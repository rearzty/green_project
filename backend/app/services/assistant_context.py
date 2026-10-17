"""Сводка плана для Юны: факты, на которые она опирается в ответах.

Без неё Юна могла только менять настройки генерации и на вопрос «почему так
мало деревьев?» отвечала бы общими словами — или выдумывала. Здесь собирается
то, что сервис уже посчитал сам: число посадок по типам, схемам и породам,
объяснение площади (какая доля участка осталась под посадку и что закрыло
остальное — geo_engine.area_budget) и, если на карте выделена одна посадка,
её обоснование со ссылкой на акт (geo_engine.compliance).

Текст короткий намеренно: Юна работает на бесплатном тарифе Groq с лимитом
токенов в минуту, и план на десятки тысяч посадок целиком туда не отправить.
Только агрегаты — несколько сотен токенов на любой размер плана.
"""

from __future__ import annotations

from collections import Counter

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

_LABELS = {**OBJECT_LABELS_RU, NEIGHBOUR_OBJECT_TYPE: "соседнее дерево", "territory": "граница участка"}


def _find_plan(project: Project, plan_id: str) -> Plan | None:
    return next((p for p in project.plans if p.id == plan_id), None)


def _plan_summary(plan: Plan) -> list[str]:
    lines = [f"Сводка текущего плана (режим {plan.scoring_mode}, рядов деревьев {plan.tree_rows}, "
             f"деревья в покрытии {'да' if plan.trees_in_paving else 'нет'}):"]
    if not plan.materialized:
        lines.append(f"- всего посадок: {plan.item_count}")
    else:
        by_type = Counter(item.planting_type for item in plan.items)
        lines.append("- всего посадок: " + (", ".join(
            f"{TYPE_LABELS.get(t, t)} {n}" for t, n in by_type.most_common()) or "0"))
        for planting_type in ("tree", "shrub"):
            items = [i for i in plan.items if i.planting_type == planting_type]
            if not items:
                continue
            schemes = []
            for label, prefix in _SCHEMES:
                count = sum(1 for i in items if prefix in i.rationale)
                if count:
                    schemes.append(f"{label} {count}")
            loose = len(items) - sum(int(s.rsplit(" ", 1)[1]) for s in schemes)
            if loose:
                schemes.append(f"россыпью {loose}")
            in_paving = sum(1 for i in items if IN_PAVING_RATIONALE_PREFIX in i.rationale)
            if in_paving:
                schemes.append(f"из них в покрытии тротуара {in_paving}")
            species = Counter(i.species for i in items).most_common(_MAX_SPECIES)
            lines.append(f"- {TYPE_LABELS[planting_type]}: " + ", ".join(schemes))
            lines.append(f"  породы: " + ", ".join(f"{name} {n}" for name, n in species))
    for budget in plan.area_budget or []:
        label = TYPE_LABELS.get(budget.get("planting_type"), budget.get("planting_type"))
        blockers = ", ".join(
            f"{b['label']} — {round(100 * b['share'])} %" for b in (budget.get("blockers") or [])[:3]
        )
        lines.append(
            f"- {label}: под посадку остаётся {budget['usable_m2']:.0f} м² из {budget['territory_m2']:.0f} "
            f"({100 * budget['usable_share']:.1f} % участка); больше всего закрывают: {blockers or '—'}"
        )
    return lines


def _selected_item(project: Project, plan: Plan, item_id: str) -> list[str]:
    if not plan.materialized:
        return []
    records = compliance_service.explain_items_by_id(project, plan, [item_id], plan_norms(plan))
    if not records:
        return []
    _, record = records[0]
    lines = [
        f"Выделенная на карте посадка: {TYPE_LABELS.get(record.planting_type, record.planting_type)}, "
        f"{record.species}, {'соответствует нормам' if record.compliant else 'НАРУШАЕТ нормы'}.",
        f"- {record.summary}",
    ]
    if record.ranking_rationale:
        lines.append(f"- почему выбрано это место: {record.ranking_rationale[:300]}")
    # Ближайшие ограничения — по запасу до нормы, самые жёсткие первыми.
    for check in sorted(record.checks, key=lambda c: c.actual_m - c.required_m)[:4]:
        lines.append(
            f"- {_LABELS.get(check.object_type, check.object_type)}: требуется {check.required_m:.2f} м, фактически {check.actual_m:.2f} м "
            f"({check.act}, {check.clause})"
        )
    return lines


def plan_context(project: Project, plan_id: str, selected_item_id: str | None = None) -> str | None:
    plan = _find_plan(project, plan_id)
    if plan is None:
        return None
    lines = _plan_summary(plan)
    if selected_item_id:
        lines += _selected_item(project, plan, selected_item_id)
    return "\n".join(lines)
