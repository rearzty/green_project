"""Живая проверка Юны против настоящей языковой модели из настроек (.env).

Юнит-тесты подменяют HTTP-вызов и не могут поймать то, что ломается только у
живой модели: не тот инструмент, вызов только первой из двух просьб, «выделила»
без вызова функции. Здесь — фиксированный набор просьб на сводке условного
плана; по каждой печатается время, число запросов к модели и команды.
Сверять глазами со столбцом «ждём». Каждый прогон — настоящие платные запросы
(на GLM-5.3 Flash — доли рубля за весь набор).

    python -m scripts.yuna_live_check                      # все просьбы по разу
    python -m scripts.yuna_live_check --repeat 3 --brief   # разброс ответов
    python -m scripts.yuna_live_check --only 0,11 --effort medium
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from backend.app.core.config import settings
from backend.app.schemas.assistant import AssistantGenerationSettings, AssistantSelection
from backend.app.services import assistant_service
from backend.app.services.assistant_context import BlockerFacts, BudgetFacts, PlanFacts, TypeFacts

CURRENT = AssistantGenerationSettings(planting_types=["tree", "shrub", "lawn"], scoring_mode="heuristic", tree_rows=1, trees_in_paving=False)
FACTS = PlanFacts(
    plan_id="p1", scoring_mode="heuristic", tree_rows=1, trees_in_paving=False, materialized=True, item_count=1084,
    planting_types=["tree", "shrub", "lawn"],
    counts={"shrub": 960, "tree": 118, "lawn": 6},
    types={
        "tree": TypeFacts("tree", 118, schemes=[("рядом вдоль улиц и границы участка", 92)], loose=26,
                          species=[("Липа мелколистная", 70), ("Клён остролистный", 48)]),
        "shrub": TypeFacts("shrub", 960, schemes=[("рядом вдоль улиц и границы участка", 900), ("куртинами", 60)],
                           species=[("Спирея японская", 600), ("Кизильник блестящий", 360)]),
    },
    lawn_area_m2=1240.0,
    budgets=[BudgetFacts("tree", usable_m2=3100.0, territory_m2=30270.0, usable_share=0.102,
                         blockers=[BlockerFacts("road", "край проезжей части", 0.41), BlockerFacts("heat_network", "теплосеть", 0.18)])],
)
THREE = AssistantSelection(count=3, trees=1, shrubs=2, lawns=0)
NOTHING = AssistantSelection(count=0, trees=0, shrubs=0, lawns=0)

# (просьба, выделение, нарушений, что ждём)
CASES = [
    ("посади кусты пореже и деревья двумя рядами, а потом выдели все деревья", NOTHING, 0, "settings (кусты реже, 2 ряда) + select trees"),
    ("сделай деревья погуще", NOTHING, 0, "settings: деревья ~3,5 м"),
    ("покажи нарушения", NOTHING, 4, "select violations"),
    ("удали выделенные", THREE, 0, "edit_selection delete"),
    ("удали все кусты", NOTHING, 0, "select shrubs, без удаления, просит подтвердить"),
    ("скачай dxf и покажи план в 3д", NOTHING, 0, "ui export_dxf + ui show_3d"),
    ("почему так мало деревьев?", NOTHING, 0, "текст по сводке: 10 % участка, дорога 41 %"),
    ("сажай деревья ближе к трубам", NOTHING, 0, "отказ: это норматив"),
    ("какая погода в Москве?", NOTHING, 0, "вежливый отказ, не по теме"),
    ("убери газон из плана", NOTHING, 0, "settings: деревья и кусты"),
    ("выдели все липы", NOTHING, 0, "select trees, порода «Липа мелколистная»"),
    ("сделай выделенные деревьями и скачай обоснование в excel", THREE, 0, "edit_selection to_tree + ui report_csv"),
    ("выдели нарушения и покажи план в 3д", NOTHING, 4, "select violations + ui show_3d"),
    ("посади деревья двумя рядами и скачай dxf", NOTHING, 0, "settings: 2 ряда + ui export_dxf"),
]


def _brief(command: dict) -> str:
    detail = command.get("target") or command.get("operation") or command.get("command") or ""
    return f"{command['type']}:{detail}" if detail else command["type"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", default="", help="номера просьб через запятую")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--effort", default=None, help="reasoning_effort вместо настроенного (off — не слать)")
    parser.add_argument("--brief", action="store_true", help="одна строка на ответ")
    args = parser.parse_args()
    if not settings.llm_configured:
        print("Языковая модель не настроена (GREENPROJECT_LLM_* в .env) — проверять нечего.", file=sys.stderr)
        return 2
    if args.effort:
        settings.llm_reasoning_effort = args.effort

    requests = [0]
    chat = assistant_service._chat

    async def counting_chat(messages):
        requests[0] += 1
        return await chat(messages)

    assistant_service._chat = counting_chat
    only = {int(x) for x in args.only.split(",") if x.strip()}
    print(f"{settings.llm_provider} · {settings.llm_model} · reasoning_effort={settings.llm_reasoning}")
    total = 0.0
    for i, (message, selection, violations, expected) in [(i, c) for i, c in enumerate(CASES) if not only or i in only] * args.repeat:
        requests[0] = 0
        started = time.perf_counter()
        try:
            reply, commands = asyncio.run(assistant_service.ask_yuna(
                message, [], CURRENT, FACTS, selection=selection, violations_count=violations, view="2d"))
            dumped = [c.model_dump(exclude_none=True) for c in commands]
        except Exception as exc:  # noqa: BLE001 -- the point is to see it
            reply, dumped = f"!! {type(exc).__name__}: {exc}", []
        elapsed = time.perf_counter() - started
        total += elapsed
        if args.brief:
            print(f"[{i}] {elapsed:.1f} с, запросов {requests[0]}: {[_brief(c) for c in dumped]} | {reply[:110]}")
        else:
            print(f"\n[{i}] {message}\n    ждём: {expected}\n    {elapsed:.1f} с, запросов {requests[0]}: "
                  f"{json.dumps(dumped, ensure_ascii=False)}\n    ответ: {reply}")
    print(f"\nвсего {total:.1f} с")
    return 0


if __name__ == "__main__":
    sys.exit(main())
