"""Юна without a language model (assistant_rules.py): deterministic answers
and commands for the requests that actually come up in a demo, in several
phrasings each, plus the safety rules both modes share.
"""

from __future__ import annotations

import pytest

from backend.app.schemas.assistant import (
    AssistantGenerationSettings,
    AssistantMessageResponse,
    AssistantSelection,
    EditSelectionCommand,
    SelectCommand,
    SettingsCommand,
    UiCommand,
)
from backend.app.services.assistant_context import (
    BlockerFacts,
    BudgetFacts,
    CheckFacts,
    PlanFacts,
    SelectedItemFacts,
    TypeFacts,
)
from backend.app.services.assistant_rules import (
    DECLINED_TEXT,
    FALLBACK_TEXT,
    HELP_TEXT,
    NO_PLAN,
    NORMS_REFUSAL,
    SPACING_LIMITS,
    UNDO_TEXT,
    answer_with_rules,
    fmt_num,
    guard_commands,
    match_species,
    normalize,
    plural,
    split_clauses,
)

DEFAULTS = {"tree": 5.0, "shrub": 0.5}
NOTHING = AssistantSelection(count=0)
THREE = AssistantSelection(count=3, trees=1, shrubs=2)


def _facts(**overrides) -> PlanFacts:
    facts = PlanFacts(
        plan_id="plan-1",
        scoring_mode="heuristic",
        tree_rows=1,
        trees_in_paving=False,
        materialized=True,
        item_count=172,
        planting_types=["tree", "shrub", "lawn"],
        counts={"shrub": 120, "tree": 40, "lawn": 12},
        types={
            "tree": TypeFacts(
                "tree", 40, [("рядом вдоль улиц и границы участка", 30), ("группами деревьев", 10)], 0, 0,
                [("Липа мелколистная", 20), ("Клён остролистный", 12), ("Берёза повислая", 8)],
            ),
            "shrub": TypeFacts(
                "shrub", 120, [("куртинами", 100)], 20, 0,
                [("Спирея японская", 60), ("Спирея серая", 40), ("Сирень обыкновенная", 20)],
            ),
        },
        lawn_area_m2=3400.4,
        budgets=[
            BudgetFacts("tree", 204.0, 60701.0, 0.0034, [
                BlockerFacts("road", "край проезжей части", 0.92),
                BlockerFacts("cable_line", "кабель силовой/связи", 0.65),
                BlockerFacts("territory_margin", "отступ от границы участка", 0.40),
                BlockerFacts("sewer", "канализация/водосток", 0.10),
            ]),
            BudgetFacts("shrub", 9000.0, 60701.0, 0.148, [BlockerFacts("road", "край проезжей части", 0.70)]),
        ],
    )
    for key, value in overrides.items():
        setattr(facts, key, value)
    return facts


def _settings(**overrides) -> AssistantGenerationSettings:
    return AssistantGenerationSettings(**{"planting_types": ["tree", "shrub", "lawn"], **overrides})


def ask(message, *, settings=None, facts="default", selection=NOTHING, violations=2, view="2d", defaults=DEFAULTS):
    return answer_with_rules(
        message,
        settings or _settings(),
        _facts() if facts == "default" else facts,
        selection,
        violations,
        view,
        defaults,
    )


def recipe(actions):
    [command] = [a for a in actions if isinstance(a, SettingsCommand)]
    return command.settings


# --- Helpers -----------------------------------------------------------------


class TestTextHelpers:
    def test_normalize_lowercases_folds_yo_decimal_comma_and_3d(self):
        assert normalize("Ещё ЁЛКИ 2,5 м в 3-Д") == "еще елки 2.5 м в 3d"

    def test_split_clauses_on_punctuation_and_conjunctions_keeps_decimals(self):
        assert split_clauses("Кусты пореже, деревья через 2,5 м и двумя рядами!") == [
            "кусты пореже", "деревья через 2.5 м", "двумя рядами",
        ]

    @pytest.mark.parametrize("n, word", [(1, "дерево"), (3, "дерева"), (11, "деревьев"), (21, "дерево"), (40, "деревьев")])
    def test_plural(self, n, word):
        assert plural(n, "дерево", "дерева", "деревьев") == word

    @pytest.mark.parametrize("value, text", [(4.2, "4,2"), (15.0, "15"), (0.35, "0,35"), (60700.0, "60700")])
    def test_fmt_num_uses_a_decimal_comma_and_drops_trailing_zeros_only(self, value, text):
        assert fmt_num(value) == text

    def test_match_species_needs_the_genus_and_prefers_the_epithet(self):
        catalogue = [("Спирея японская", 60), ("Спирея серая", 40), ("Липа мелколистная", 20)]
        assert match_species("спирею серую", catalogue) == ["Спирея серая", "Спирея японская"]
        assert match_species("липы", catalogue) == ["Липа мелколистная"]
        assert match_species("японская", catalogue) == []  # an epithet alone isn't a species


# --- Help, greeting, fallback --------------------------------------------------


class TestHelpAndFallback:
    @pytest.mark.parametrize("message", ["что ты умеешь?", "Помощь", "help", "какие команды ты понимаешь", "?"])
    def test_help(self, message):
        reply, actions = ask(message)
        assert reply == HELP_TEXT and actions == []

    @pytest.mark.parametrize("message", ["Привет!", "здравствуйте", "Добрый день"])
    def test_greeting_alone_introduces_herself(self, message):
        reply, _ = ask(message)
        assert reply.startswith("Привет! Я Юна") and "базовом режиме" in reply

    def test_greeting_with_a_request_does_the_request(self):
        reply, actions = ask("Юна, привет! Сделай кусты пореже")
        assert reply == "Привет! Интервал кустов в живой изгороди 0,5 → 0,7 м, перестраиваю план."
        assert recipe(actions).shrub_spacing_m == 0.7

    def test_thanks(self):
        assert ask("спасибо!")[0].startswith("Пожалуйста")

    @pytest.mark.parametrize("message", ["спасибо, удачи!", "удачи!", "до свидания"])
    def test_farewell_is_answered_and_never_edits(self, message):
        # «Спасибо, удачи!» used to be typo-corrected into «спасибо, удали!» and
        # deleted the three selected objects.
        reply, actions = ask(message, selection=THREE)
        assert actions == [] and reply.startswith(("Пожалуйста", "Спасибо"))

    @pytest.mark.parametrize("message", ["расскажи анекдот", "какая погода в Москве?", "ок", "", "   ", "!!!", "🙂🙂", "ы" * 5000])
    def test_unknown_gets_the_fallback_that_says_it_is_basic_mode(self, message):
        reply, actions = ask(message)
        assert actions == []
        assert reply in (FALLBACK_TEXT, HELP_TEXT)
        assert "базовом режиме" in reply


# --- Questions about the plan ---------------------------------------------------


class TestWhyFew:
    @pytest.mark.parametrize(
        "message",
        ["Почему так мало деревьев?", "почему деревьев мало", "почему нет деревьев", "мало деревьев", "почему одни кусты?"],
    )
    def test_trees_explained_from_the_area_budget(self, message):
        reply, actions = ask(message)
        assert actions == []
        assert "В плане 40 деревьев" in reply
        assert "204 м² из 60 701 м² (0,3 % участка)" in reply
        assert "край проезжей части — 92 %" in reply and "кабель силовой/связи — 65 %" in reply
        assert "канализация" not in reply  # only the top three blockers
        assert "СП 42.13330.2016" in reply

    def test_suggests_trees_in_paving_and_ml_when_they_are_off(self):
        reply, _ = ask("почему так мало деревьев?")
        assert "разреши деревья в тротуаре" in reply and "режим ML" in reply

    def test_no_suggestions_for_what_is_already_on(self):
        reply, _ = ask("почему так мало деревьев?", settings=_settings(trees_in_paving=True, scoring_mode="ml"))
        assert "тротуар" not in reply and "ML" not in reply

    def test_shrubs_get_their_own_budget_without_the_paving_hint(self):
        reply, _ = ask("почему так мало кустов")
        assert "В плане 120 кустов" in reply and "9 000 м²" in reply and "14,8 %" in reply
        assert "тротуар" not in reply

    def test_roomy_plot_offers_the_planting_schemes_not_a_denser_interval(self):
        # Trees already stand at their crown-class norm: «почаще» would change
        # nothing, so the advice is what can add trees -- and only what is off.
        facts = _facts(budgets=[BudgetFacts("tree", 3611.0, 8000.0, 0.451, [BlockerFacts("building", "стена здания", 0.23)])])
        reply, _ = ask("почему так мало деревьев?", facts=facts)
        assert "3 611 м² из 8 000 м² (45,1 % участка)" in reply
        assert "гуще нормы класса кроны их не поставить" in reply
        assert "«деревья двумя рядами»" in reply and "«разреши деревья в тротуаре»" in reply
        assert "почаще" not in reply and "норматив" not in reply

        reply, _ = ask("почему так мало деревьев?", facts=facts, settings=_settings(tree_rows=2))
        assert "двумя рядами" not in reply and "«разреши деревья в тротуаре»" in reply

    def test_roomy_plot_with_a_user_floor_suggests_lowering_it(self):
        facts = _facts(budgets=[BudgetFacts("tree", 3611.0, 8000.0, 0.451, [])])
        reply, _ = ask("почему так мало деревьев?", facts=facts, settings=_settings(tree_spacing_m=7.0))
        assert "не меньше 7 м" in reply and "«деревья почаще»" in reply

    def test_roomy_shrubs_get_the_hedge_hint(self):
        facts = _facts(budgets=[BudgetFacts("shrub", 9000.0, 20000.0, 0.45, [])])
        assert "живая изгородь станет гуще" in ask("почему так мало кустов?", facts=facts)[0]
        assert "изгородь" not in ask("почему так мало кустов?", facts=facts, settings=_settings(shrub_spacing_m=0.3))[0]

    def test_a_pruned_plan_does_not_claim_there_are_no_trees(self):
        facts = _facts(materialized=False, counts={}, types={})
        reply, _ = ask("почему так мало деревьев?", facts=facts)
        assert "нет" not in reply.split(":")[0] and reply.startswith("Под деревья по нормам остаётся 204 м²")

    def test_a_type_switched_off_in_the_settings_is_named_as_the_reason(self):
        facts = _facts(counts={"shrub": 120}, types={})
        reply, _ = ask("почему нет деревьев?", settings=_settings(planting_types=["shrub", "lawn"]), facts=facts)
        assert reply.startswith("Деревья выключены в настройках")

    def test_without_a_plan(self):
        assert ask("почему так мало деревьев?", facts=None)[0] == NO_PLAN


class TestWhyHere:
    COMPLIANT = SelectedItemFacts(
        planting_type="tree",
        species="Липа мелколистная",
        compliant=True,
        summary="Дерево: посадка допустима…",
        ranking_rationale="Рядовая посадка вдоль линейного ориентира. Шаг 6 м.",
        checks=[
            CheckFacts("road", "край проезжей части", 2.0, 2.35, True, "СП 42.13330.2016", "п. 9.6, таблица 9.1"),
            CheckFacts("cable_line", "кабель силовой/связи", 2.0, 4.1, True, "СП 42.13330.2016", "п. 9.6, таблица 9.1"),
        ],
    )
    VIOLATING = SelectedItemFacts(
        planting_type="tree",
        species="Липа мелколистная",
        compliant=False,
        summary="Дерево: посадка НЕ допустима…",
        checks=[
            CheckFacts("gas_pipe", "газопровод", 2.4, 1.0, False,
                       "Постановление Правительства Москвы № 743-ПП от 10.09.2002 (ред. от 05.06.2024)", "п. 3.6.3, таблица 3.6.1"),
        ],
    )

    @pytest.mark.parametrize("message", ["Почему эта посадка здесь?", "почему она тут стоит", "обоснуй эту посадку", "почему это дерево здесь"])
    def test_selected_item_is_explained_with_the_binding_norm(self, message):
        reply, actions = ask(message, facts=_facts(selected=self.COMPLIANT), selection=AssistantSelection(count=1, trees=1))
        assert actions == []
        assert reply.startswith("Липа мелколистная (дерево) стоит по нормам.")
        assert "край проезжей части: нужно 2 м, здесь 2,35 м (СП 42.13330.2016, п. 9.6, таблица 9.1)" in reply
        # Mid-sentence, so lower case; the card's real section title, not «Почему здесь?».
        assert "Место выбрано так: рядовая посадка вдоль линейного ориентира." in reply
        assert reply.endswith("Все отступы — в карточке посадки, раздел «Почему здесь можно».")

    def test_violating_item_names_the_broken_setback_with_a_short_act(self):
        reply, _ = ask("почему здесь?", facts=_facts(selected=self.VIOLATING), selection=AssistantSelection(count=1, trees=1))
        assert "нарушает нормы: газопровод — нужно 2,4 м, а здесь 1 м (743-ПП, п. 3.6.3, таблица 3.6.1)" in reply
        assert "раздел «Нормативные отступы»" in reply

    def test_a_violation_is_blamed_on_a_manual_edit_only_when_there_was_one(self):
        from dataclasses import replace

        selection = AssistantSelection(count=1, trees=1)
        untouched = ask("почему здесь?", facts=_facts(selected=replace(self.VIOLATING, is_manual_edit=False)), selection=selection)[0]
        edited = ask("почему здесь?", facts=_facts(selected=replace(self.VIOLATING, is_manual_edit=True)), selection=selection)[0]
        assert "вручную" not in untouched and "правк" not in untouched
        assert "Посадку правили вручную" in edited

    def test_distances_are_rounded_like_the_card(self):
        from dataclasses import replace

        far = CheckFacts("building", "стена здания", 5.0, 14.16, True, "СП 42.13330.2016", "п. 9.6, таблица 9.1")
        reply, _ = ask("почему здесь?", facts=_facts(selected=replace(self.COMPLIANT, checks=[far])),
                       selection=AssistantSelection(count=1, trees=1))
        assert "здесь 14,2 м" in reply  # InspectorCard.tsx `meters`: one digit from 10 m up

    def test_a_lawn_has_no_species_name(self):
        lawn = SelectedItemFacts(planting_type="lawn", species="default", compliant=True, summary="Газон: допустимо.")
        reply, _ = ask("почему здесь?", facts=_facts(selected=lawn), selection=AssistantSelection(count=1, lawns=1))
        assert reply.startswith("Этот участок газона стоит по нормам") and "default" not in reply

    def test_nothing_selected_asks_to_select_one(self):
        reply, _ = ask("почему эта посадка здесь?")
        assert reply.startswith("Выделите на карте одну посадку")

    def test_several_selected_asks_to_pick_one(self):
        reply, _ = ask("почему они здесь?", selection=THREE)
        assert reply.startswith("Выделено 3 объекта — выберите кликом одну посадку")


class TestCounts:
    @pytest.mark.parametrize(
        "message, expected",
        [
            ("сколько деревьев?", "Деревьев в плане 40: рядом вдоль улиц и границы участка 30, группами деревьев 10."),
            ("Сколько кустов", "Кустов в плане 120: куртинами 100, россыпью 20."),
            ("сколько газона", "Газона в плане 12 участков — 3 400 м²."),
            ("сколько всего посадок", "В плане 172 посадки: 40 деревьев, 120 кустов и 12 участков газона."),
            ("сколько лип?", "Липа мелколистная — 20 деревьев."),
            ("сколько нарушений", "В плане 2 нарушения — скажите «покажи нарушения», и я их выделю."),
            ("сколько выделено?", "Сейчас ничего не выделено."),
        ],
    )
    def test_counts(self, message, expected):
        reply, actions = ask(message)
        assert reply == expected and actions == []

    def test_two_types_in_one_question(self):
        reply, _ = ask("сколько деревьев и кустов?")
        assert reply.startswith("Деревьев в плане 40") and "Кустов в плане 120" in reply

    def test_species_count(self):
        reply, _ = ask("сколько пород?")
        assert reply.startswith("У деревьев — 3 породы (Липа мелколистная 20, Клён остролистный 12")
        assert "у кустов — 3 породы (Спирея японская 60" in reply

    def test_selection_count(self):
        assert ask("сколько выделено", selection=THREE)[0] == "Выделено 3 объекта: 1 дерево и 2 куста."

    def test_species_list(self):
        reply, _ = ask("какие породы деревьев?")
        assert reply == "Деревья: Липа мелколистная — 20, Клён остролистный — 12, Берёза повислая — 8."

    def test_settings_summary(self):
        reply, actions = ask("какие сейчас настройки?", settings=_settings(tree_spacing_m=7.0))
        assert actions == []
        assert "интервал деревьев — не меньше 7 м, кустов — живая изгородь 0,5 м (по умолчанию)" in reply
        assert "режим эвристики" in reply

    def test_settings_summary_without_own_intervals_tells_the_norm(self):
        reply, _ = ask("какие сейчас настройки?")
        assert "интервал деревьев — по норме класса кроны (МГСН 1.02-02 п. 4.2.9.2: широкая крона — 8 м" in reply


class TestViolations:
    @pytest.mark.parametrize("message", ["покажи нарушения", "где нарушения?", "выдели нарушения", "есть нарушения?"])
    def test_violations_are_selected(self, message):
        reply, actions = ask(message, violations=3)
        assert actions == [SelectCommand(target="violations")]
        assert reply.startswith("Выделила 3 нарушения.")

    def test_no_violations(self):
        reply, actions = ask("покажи нарушения", violations=0)
        assert actions == [] and reply == "Нарушений нет — все посадки проходят по нормам."

    def test_not_checked_yet(self):
        reply, actions = ask("покажи нарушения", violations=None)
        assert actions == [] and "ещё не готова" in reply

    def test_why_violations_names_manual_edits_when_there_are_some(self):
        reply, actions = ask("почему нарушения?", violations=2, facts=_facts(manual_edits=3))
        assert actions == [SelectCommand(target="violations")]
        assert "3 посадки в плане изменены вручную" in reply

    def test_why_violations_does_not_blame_edits_that_never_happened(self):
        reply, actions = ask("почему нарушения?", violations=2, facts=_facts(manual_edits=0))
        assert actions == [SelectCommand(target="violations")]
        assert "Ручных правок в плане нет" in reply and "изменены вручную" not in reply
        # Unknown (old facts): no cause is made up, the card shows it.
        reply, _ = ask("почему нарушения?", violations=2)
        assert "правк" not in reply and reply.startswith("Выделила 2 нарушения.")

    def test_one_violation_is_not_any_of_many(self):
        reply, _ = ask("покажи нарушения", violations=1)
        assert reply == "Выделила 1 нарушение. Какой отступ нарушен — в карточке посадки."
        assert "по любому" in ask("покажи нарушения", violations=3)[0]


# --- Generation settings --------------------------------------------------------


class TestSpacing:
    @pytest.mark.parametrize(
        "message",
        ["кусты пореже", "разреди кусты", "посади кусты реже", "кусты подальше друг от друга", "не так густо кусты",
         "меньше кустов", "увеличь интервал кустов"],
    )
    def test_sparser_shrubs(self, message):
        # Without an own interval the shrubs are a hedge at 0,5 m
        # (planner.IN_ROW_PITCH_M), not planting_norms.yaml's 3 m.
        reply, actions = ask(message)
        assert recipe(actions).shrub_spacing_m == 0.7
        assert reply == "Интервал кустов в живой изгороди 0,5 → 0,7 м, перестраиваю план."

    def test_denser_shrubs_down_to_the_low_hedge_bound(self):
        reply, actions = ask("кусты погуще")
        assert recipe(actions).shrub_spacing_m == 0.35 and "0,5 → 0,35 м" in reply
        assert recipe(ask("кусты погуще", settings=_settings(shrub_spacing_m=0.35))[1]).shrub_spacing_m == 0.3
        reply, actions = ask("кусты погуще", settings=_settings(shrub_spacing_m=0.3))
        assert actions == [] and reply == (
            "Интервал кустов уже минимальный — 0,3 м: нижняя граница 743-ПП табл. 3.6.2 для низкой живой изгороди."
        )

    @pytest.mark.parametrize(
        "message", ["деревья гуще", "погуще деревья", "плотнее деревья", "деревья ближе друг к другу", "больше деревьев",
                    "уменьши расстояние между деревьями", "посади деревья почаще", "сделай деревья в 2 раза гуще"],
    )
    def test_denser_trees_without_an_own_interval_change_nothing_and_say_why(self, message):
        # A user interval is a floor over the crown-class norm: trees can't
        # stand denser than 3/5/8 m. «5 → 3,5 м» used to be claimed, and
        # measured on three synthetic plots it changed nothing.
        reply, actions = ask(message)
        assert actions == []
        assert reply.startswith("Гуще нормы деревья не поставить")
        assert "МГСН 1.02-02 п. 4.2.9.2: широкая крона — 8 м, средняя — 5 м, узкая — 3 м" in reply
        assert "«деревья двумя рядами»" in reply and "«разреши деревья в тротуаре»" in reply

    def test_the_denser_trees_answer_offers_only_what_is_off(self):
        reply, _ = ask("деревья гуще", settings=_settings(tree_rows=2))
        assert "двумя рядами" not in reply and "«разреши деревья в тротуаре»" in reply
        reply, _ = ask("деревья гуще", settings=_settings(tree_rows=2, trees_in_paving=True))
        assert reply.endswith("Двухрядная аллея и деревья в покрытии тротуара уже включены.")
        # Switched on in the same message -- not offered again.
        reply, actions = ask("деревья погуще и двумя рядами")
        assert recipe(actions).tree_rows == 2 and "«деревья двумя рядами»" not in reply

    def test_an_own_interval_at_or_below_the_narrow_norm_is_the_norm(self):
        assert ask("деревья гуще", settings=_settings(tree_spacing_m=3.0))[1] == []
        assert ask("деревья гуще", settings=_settings(tree_spacing_m=2.0))[0].startswith("Гуще нормы деревья не поставить")

    def test_denser_trees_lower_an_own_floor_and_drop_it_at_the_norm(self):
        reply, actions = ask("деревья гуще", settings=_settings(tree_spacing_m=6.0))
        assert recipe(actions).tree_spacing_m == 4.2
        assert reply == (
            "Интервал деревьев — не меньше 4,2 м вместо 6 (средним кронам норма — 5 м, широким — 8 м), перестраиваю план."
        )
        reply, actions = ask("деревья гуще", settings=_settings(tree_spacing_m=4.0))
        assert recipe(actions).tree_spacing_m is None  # 2,8 m is below every crown norm
        assert reply == "Интервал деревьев — по норме класса кроны (3–8 м), перестраиваю план."

    def test_sparser_trees_without_an_own_interval_start_from_the_medium_crown(self):
        reply, actions = ask("деревья пореже")
        assert recipe(actions).tree_spacing_m == 7.0
        assert reply == "Интервал деревьев — не меньше 7 м (широким кронам норма — 8 м), перестраиваю план."

    def test_an_explicit_tree_interval_is_a_floor(self):
        reply, actions = ask("интервал деревьев 6 м")
        assert recipe(actions).tree_spacing_m == 6.0
        assert reply == "Интервал деревьев — не меньше 6 м (широким кронам норма — 8 м), перестраиваю план."
        assert "кронам" not in ask("интервал деревьев 9 м")[0]  # above every norm: every tree at 9 m

    @pytest.mark.parametrize("message", ["интервал деревьев 2 м", "деревья через 3 метра", "интервал деревьев 0,2 м"])
    def test_an_explicit_tree_interval_below_the_norm_is_the_norm(self, message):
        reply, actions = ask(message)
        assert actions == [] and "это интервал по норме" in reply
        reply, actions = ask(message, settings=_settings(tree_spacing_m=7.0))
        assert recipe(actions).tree_spacing_m is None

    @pytest.mark.parametrize(
        "message, value",
        [
            ("интервал деревьев 6 м", 6.0),
            ("деревья через 6 метров", 6.0),
            ("шаг деревьев 6", 6.0),
            ("расстояние между деревьями 6 метров", 6.0),
            ("деревья каждые 6 м", 6.0),
            ("интервал деревьев шесть метров", 6.0),
            ("Интервал деревьев 7,5 м", 7.5),
        ],
    )
    def test_explicit_tree_interval(self, message, value):
        _, actions = ask(message)
        assert recipe(actions).tree_spacing_m == value and recipe(actions).shrub_spacing_m is None

    @pytest.mark.parametrize("message, value", [("кусты через 2,5 метра", 2.5), ("через полтора метра кусты", 1.5)])
    def test_explicit_shrub_interval(self, message, value):
        _, actions = ask(message)
        assert recipe(actions).shrub_spacing_m == value

    def test_relative_change_starts_from_the_hedge_pitch_when_spacing_is_unset(self):
        reply, actions = ask("кусты пореже", settings=_settings(shrub_spacing_m=None))
        assert recipe(actions).shrub_spacing_m == 0.7 and "0,5 → 0,7" in reply

    def test_relative_change_uses_the_defaults_it_is_given(self):
        _, actions = ask("кусты пореже", defaults={"tree": 5.0, "shrub": 2.0})
        assert recipe(actions).shrub_spacing_m == 2.8

    def test_relative_change_starts_from_the_current_value(self):
        reply, actions = ask("деревья пореже", settings=_settings(tree_spacing_m=7.0))
        assert recipe(actions).tree_spacing_m == 9.8 and "не меньше 9,8 м вместо 7" in reply

    @pytest.mark.parametrize(
        "message, field, value, said",
        [("интервал деревьев 20 м", "tree_spacing_m", 15.0, "можно до 15 м"),
         ("кусты через 0,1 м", "shrub_spacing_m", 0.3, "можно от 0,3 до 10 м"),
         ("кусты через 50 метров", "shrub_spacing_m", 10.0, "можно от 0,3 до 10 м")],
    )
    def test_explicit_values_are_clamped_to_the_generate_bounds(self, message, field, value, said):
        reply, actions = ask(message)
        assert getattr(recipe(actions), field) == value
        assert said in reply

    def test_already_at_the_limit_changes_nothing(self):
        reply, actions = ask("деревья пореже", settings=_settings(tree_spacing_m=15.0))
        assert actions == [] and reply == "Интервал деревьев уже максимальный — 15 м."

    def test_twice_as_sparse_and_a_metre_delta(self):
        assert recipe(ask("сделай деревья в 2 раза реже")[1]).tree_spacing_m == 10.0
        assert recipe(ask("увеличь интервал деревьев на 2 метра")[1]).tree_spacing_m == 7.0
        assert recipe(ask("чуть реже деревья")[1]).tree_spacing_m == 6.0

    def test_no_subject_relative_change_applies_to_trees_and_shrubs(self):
        reply, actions = ask("посади погуще")
        settings = recipe(actions)
        assert settings.tree_spacing_m is None and settings.shrub_spacing_m == 0.35
        assert "Гуще нормы деревья не поставить" in reply
        settings = recipe(ask("посади пореже")[1])
        assert settings.tree_spacing_m == 7.0 and settings.shrub_spacing_m == 0.7

    def test_no_subject_explicit_number_asks_which(self):
        reply, actions = ask("интервал 4 м")
        assert actions == [] and reply.startswith("Для деревьев или кустов?")

    def test_a_switched_off_type_is_not_silently_tuned(self):
        reply, actions = ask("кусты пореже", settings=_settings(planting_types=["tree", "lawn"]))
        assert actions == [] and "добавь кусты" in reply


class TestRowsPavingTypesMode:
    @pytest.mark.parametrize("message", ["посади деревья двумя рядами", "в два ряда", "двухрядная посадка", "давай 2 ряда"])
    def test_two_rows(self, message):
        _, actions = ask(message)
        assert recipe(actions).tree_rows == 2

    @pytest.mark.parametrize("message", ["в один ряд", "однорядная аллея", "убери второй ряд"])
    def test_one_row(self, message):
        _, actions = ask(message, settings=_settings(tree_rows=2))
        assert recipe(actions).tree_rows == 1

    def test_rows_already_set(self):
        reply, actions = ask("двумя рядами", settings=_settings(tree_rows=2))
        assert actions == [] and reply == "Деревья и так двумя рядами."

    @pytest.mark.parametrize("message", ["разреши деревья в тротуаре", "сажай деревья в покрытии", "можно деревья в асфальте?"])
    def test_trees_in_paving_on(self, message):
        reply, actions = ask(message)
        assert recipe(actions).trees_in_paving is True
        assert reply == "Деревья в покрытии тротуара разрешены, перестраиваю план."

    @pytest.mark.parametrize("message", ["не сажай деревья в тротуаре", "запрети деревья в покрытии", "убери деревья из тротуара"])
    def test_trees_in_paving_off(self, message):
        _, actions = ask(message, settings=_settings(trees_in_paving=True))
        assert recipe(actions).trees_in_paving is False

    @pytest.mark.parametrize(
        "message, types",
        [
            ("без газона", ["tree", "shrub"]),
            ("убери кусты", ["tree", "lawn"]),
            ("только деревья", ["tree"]),
            ("оставь только деревья и кусты", ["tree", "shrub"]),
            ("без газона и кустов", ["tree"]),
            ("не нужен газон", ["tree", "shrub"]),
        ],
    )
    def test_planting_types(self, message, types):
        _, actions = ask(message)
        assert recipe(actions).planting_types == types

    def test_add_a_type(self):
        reply, actions = ask("добавь газон", settings=_settings(planting_types=["tree"]))
        assert recipe(actions).planting_types == ["tree", "lawn"]
        assert reply == "С газоном, перестраиваю план."

    def test_cannot_remove_the_last_type(self):
        reply, actions = ask("убери деревья", settings=_settings(planting_types=["tree"]))
        assert actions == [] and "без посадок план не построить" in reply

    @pytest.mark.parametrize("message", ["режим ML", "включи модель", "как проектировщик", "переключи на мл"])
    def test_ml_mode(self, message):
        _, actions = ask(message)
        assert recipe(actions).scoring_mode == "ml"

    @pytest.mark.parametrize("message", ["эвристика", "выключи ML", "вернись к эвристике", "по правилам"])
    def test_heuristic_mode(self, message):
        reply, actions = ask(message, settings=_settings(scoring_mode="ml"))
        assert recipe(actions).scoring_mode == "heuristic"
        assert "2D" not in reply  # «вернись» here is about the mode, not the map

    @pytest.mark.parametrize("message", ["сгенерируй план", "пересчитай", "построй план заново", "ещё раз"])
    def test_regenerate_with_the_same_settings(self, message):
        reply, actions = ask(message)
        assert recipe(actions) == recipe([SettingsCommand(settings=_settings().model_dump())])
        assert reply == "Перестраиваю план с текущими настройками."


class TestCombinedRequests:
    def test_shrubs_sparser_and_two_rows_is_one_settings_command(self):
        reply, actions = ask("кусты пореже и двумя рядами")
        assert len(actions) == 1
        settings = recipe(actions)
        assert settings.shrub_spacing_m == 0.7 and settings.tree_rows == 2
        assert reply == "Интервал кустов в живой изгороди 0,5 → 0,7 м, деревья двумя рядами, перестраиваю план."

    def test_different_changes_for_different_types(self):
        settings = recipe(ask("деревья пореже, а кусты погуще")[1])
        assert settings.tree_spacing_m == 7.0 and settings.shrub_spacing_m == 0.35

    def test_shared_predicate_for_two_subjects(self):
        for message in ("деревья и кусты пореже", "пореже деревья и кусты"):
            settings = recipe(ask(message)[1])
            assert settings.tree_spacing_m == 7.0 and settings.shrub_spacing_m == 0.7

    def test_four_changes_at_once(self):
        settings = recipe(ask("кусты погуще, деревья реже, без газона и двумя рядами")[1])
        assert (settings.shrub_spacing_m, settings.tree_spacing_m, settings.planting_types, settings.tree_rows) == (
            0.35, 7.0, ["tree", "shrub"], 2,
        )

    def test_settings_and_a_view_switch_keep_their_order(self):
        reply, actions = ask("сделай деревья пореже и покажи в 3D")
        assert [a.type for a in actions] == ["settings", "ui"]
        assert reply == (
            "Интервал деревьев — не меньше 7 м (широким кронам норма — 8 м), перестраиваю план. Показываю план в 3D."
        )


# --- Selection and edits ---------------------------------------------------------


class TestEditSelection:
    @pytest.mark.parametrize("message", ["удали выделенные", "удали их", "удали это", "убери выделенные", "удали"])
    def test_delete_selected(self, message):
        reply, actions = ask(message, selection=THREE)
        assert actions == [EditSelectionCommand(operation="delete")]
        assert reply == "Удаляю выделенное (3 объекта) — отменить можно Ctrl+Z."

    @pytest.mark.parametrize("message", ["удали выделенные", "сделай их деревьями", "в кусты"])
    def test_nothing_selected_asks_to_select_first(self, message):
        reply, actions = ask(message, selection=NOTHING)
        assert actions == []
        assert reply.startswith("Сейчас на карте ничего не выделено")

    @pytest.mark.parametrize(
        "message, operation",
        [("сделай их деревьями", "to_tree"), ("преврати в деревья", "to_tree"), ("в кусты", "to_shrub"),
         ("сделай их кустами", "to_shrub"), ("замени на кусты", "to_shrub")],
    )
    def test_retype_selected(self, message, operation):
        _, actions = ask(message, selection=THREE)
        assert actions == [EditSelectionCommand(operation=operation)]

    def test_lawn_cannot_become_a_tree(self):
        reply, actions = ask("сделай их деревьями", selection=AssistantSelection(count=2, lawns=2))
        assert actions == [] and "Газон — это площадь" in reply

    def test_mass_retype_is_two_steps(self):
        reply, actions = ask("сделай все кусты деревьями", selection=THREE)
        assert actions == [SelectCommand(target="shrubs")]
        assert reply == "Выделила все кусты (120) — скажите «сделай их деревьями», если хотите поменять тип."


class TestSelect:
    @pytest.mark.parametrize(
        "message, command",
        [
            ("выдели все деревья", SelectCommand(target="trees")),
            ("выбери кусты", SelectCommand(target="shrubs")),
            ("выдели газон", SelectCommand(target="lawn")),
            ("выдели всё", SelectCommand(target="all")),
            ("выдели все объекты", SelectCommand(target="all")),
            ("где деревья?", SelectCommand(target="trees")),
            ("выдели липы", SelectCommand(target="trees", species="Липа мелколистная")),
            ("покажи все клёны", SelectCommand(target="trees", species="Клён остролистный")),
            ("выдели сирень", SelectCommand(target="shrubs", species="Сирень обыкновенная")),
        ],
    )
    def test_select(self, message, command):
        _, actions = ask(message)
        assert actions == [command]

    def test_species_absent_from_the_plan(self):
        reply, actions = ask("выдели дубы")
        assert actions == [] and reply.startswith("Породы «Дуб черешчатый» в плане нет — есть: Липа мелколистная")

    def test_genus_with_several_species_names_the_others(self):
        reply, actions = ask("выдели спиреи")
        assert actions == [SelectCommand(target="shrubs", species="Спирея японская")]
        assert "«Спирея серая» (40)" in reply

    @pytest.mark.parametrize("message", ["сними выделение", "убери выделение", "сбрось выбор"])
    def test_deselect(self, message):
        _, actions = ask(message, selection=THREE)
        assert actions == [SelectCommand(target="none")]

    def test_deselect_with_nothing_selected(self):
        reply, actions = ask("сними выделение")
        assert actions == [] and reply == "Ничего и так не выделено."

    def test_select_without_a_plan(self):
        reply, actions = ask("выдели все деревья", facts=None)
        assert actions == [] and "Плана пока нет" in reply


class TestSafetyRules:
    def test_select_then_delete_in_one_message_only_selects(self):
        reply, actions = ask("выдели все кусты и удали их", selection=THREE)
        assert actions == [SelectCommand(target="shrubs")]
        assert reply == "Выделила все кусты (120) — скажите «удали выделенные», если хотите удалить."

    def test_delete_everything_is_select_and_confirm(self):
        reply, actions = ask("удали всё", selection=THREE)
        assert actions == [SelectCommand(target="all")]
        assert "скажите «удали выделенные»" in reply


class TestDeleteAGroupMeansTheMap:
    """«Удали все кусты» -- one meaning in both modes: select the group, show
    it, delete on the second request (the model's prompt already said so;
    rules mode used to regenerate the plan without shrubs). Removing a type
    from the recipe is «убери кусты», «без кустов», «убери/удали кусты из плана»."""

    @pytest.mark.parametrize(
        "message, command, said",
        [
            ("удали все кусты", SelectCommand(target="shrubs"), "Выделила все кусты (120)"),
            ("удали кусты", SelectCommand(target="shrubs"), "Выделила все кусты (120)"),
            ("удали деревья", SelectCommand(target="trees"), "Выделила все деревья (40)"),
            ("удали газон", SelectCommand(target="lawn"), "Выделила весь газон (12 участков)"),
            ("удали липы", SelectCommand(target="trees", species="Липа мелколистная"), "Выделила породу «Липа мелколистная»"),
            ("сотри все спиреи серые", SelectCommand(target="shrubs", species="Спирея серая"), "«Спирея серая»"),
        ],
    )
    def test_select_and_confirm(self, message, command, said):
        for selection in (NOTHING, THREE):
            reply, actions = ask(message, selection=selection)
            assert actions == [command]
            assert said in reply and "скажите «удали выделенные», если хотите удалить" in reply

    def test_an_absent_species_is_said_and_nothing_is_deleted(self):
        reply, actions = ask("удали дубы", selection=THREE)
        assert actions == [] and reply.startswith("Породы «Дуб черешчатый» в плане нет")

    def test_an_absent_type_is_said(self):
        reply, actions = ask("удали все кусты", selection=THREE, facts=NO_SHRUBS)
        assert actions == [] and reply == "Кустов в плане нет — выделять нечего."

    @pytest.mark.parametrize(
        "message, types",
        [("убери кусты", ["tree", "lawn"]), ("убери все кусты", ["tree", "lawn"]), ("без кустов", ["tree", "lawn"]),
         ("убери кусты из плана", ["tree", "lawn"]), ("удали кусты из плана", ["tree", "lawn"]),
         ("удали газон из генерации", ["tree", "shrub"])],
    )
    def test_the_recipe_wording_still_changes_the_planting_types(self, message, types):
        assert recipe(ask(message, selection=THREE)[1]).planting_types == types

    def test_guard_drops_edit_without_selection(self):
        kept, dropped = guard_commands([EditSelectionCommand(operation="delete"), UiCommand(command="show_3d")], None)
        assert kept == [UiCommand(command="show_3d")]
        assert [(d.operation, d.reason) for d in dropped] == [("delete", "nothing_selected")]

    def test_guard_drops_every_edit_next_to_a_select_retype_too(self):
        # Mass edits are always two steps -- a retype of a group selected in
        # the same answer used to go through.
        commands = [SelectCommand(target="shrubs"), EditSelectionCommand(operation="delete"), EditSelectionCommand(operation="to_tree")]
        kept, dropped = guard_commands(commands, THREE)
        assert kept == [SelectCommand(target="shrubs")]
        assert [(d.operation, d.reason) for d in dropped] == [("delete", "after_select"), ("to_tree", "after_select")]

    def test_guard_drops_edits_after_a_select_that_failed(self):
        kept, dropped = guard_commands([EditSelectionCommand(operation="delete")], THREE, select_attempted=True)
        assert kept == [] and [(d.operation, d.reason) for d in dropped] == [("delete", "select_failed")]

    def test_guard_drops_an_edit_that_was_only_asked_about(self):
        commands = [EditSelectionCommand(operation="delete"), UiCommand(command="show_3d")]
        kept, dropped = guard_commands(commands, THREE, asked={"delete"})
        assert kept == [UiCommand(command="show_3d")] and [d.reason for d in dropped] == ["asked"]

    def test_guard_keeps_the_selection_when_deselect_and_edit_come_together(self):
        commands = [SelectCommand(target="none"), EditSelectionCommand(operation="delete")]
        kept, dropped = guard_commands(commands, THREE)
        assert kept == [] and [d.reason for d in dropped] == ["with_deselect"]
        # Nothing selected -- nothing to keep, the deselect stays harmless.
        kept, dropped = guard_commands(commands, NOTHING)
        assert kept == [SelectCommand(target="none")] and [d.reason for d in dropped] == ["nothing_selected"]


NO_SHRUBS = _facts(counts={"tree": 40, "lawn": 12}, types={k: v for k, v in _facts().types.items() if k == "tree"})


class TestAFailedSelectNeverEditsTheCurrentSelection:
    """«Выдели дубы и удали их» without oaks left a bare «удали их», which
    deleted whatever was selected at the time. Any select attempt in the
    message -- successful or not -- turns every edit into a confirmation."""

    @pytest.mark.parametrize(
        "message, facts, violations, said",
        [
            ("выдели дубы и удали их", "default", 2, "Породы «Дуб черешчатый» в плане нет"),
            ("выдели кусты и удали их", NO_SHRUBS, 2, "Кустов в плане нет — выделять нечего."),
            ("покажи нарушения и удали их", "default", 0, "Нарушений нет"),
            ("покажи нарушения и удали их", "default", None, "ещё не готова"),
            ("выдели все липы, а потом удали", "default", 2, None),
            ("выдели деревья у дороги и удали их", "default", 2, "по месту"),
            ("выдели и удали", "default", 2, "Что выделить?"),
        ],
    )
    def test_delete(self, message, facts, violations, said):
        reply, actions = ask(message, selection=THREE, facts=facts, violations=violations)
        assert not any(isinstance(a, EditSelectionCommand) for a in actions)
        if said:
            assert said in reply and reply.endswith("Удалять ничего не стала.")

    @pytest.mark.parametrize("message", ["выдели дубы и сделай их кустами", "выдели дубы, сделай их деревьями"])
    def test_retype(self, message):
        reply, actions = ask(message, selection=THREE)
        assert actions == [] and reply.endswith("Тип не меняла.")

    def test_a_successful_select_asks_for_the_second_step_retype_too(self):
        reply, actions = ask("выдели все липы и сделай их кустами", selection=THREE)
        assert actions == [SelectCommand(target="trees", species="Липа мелколистная")]
        assert "скажите «сделай их кустами»" in reply

    def test_the_selection_itself_is_still_edited_in_one_step(self):
        assert ask("удали выделенные", selection=THREE)[1] == [EditSelectionCommand(operation="delete")]
        assert ask("сделай их кустами", selection=AssistantSelection(count=2, trees=2))[1] == [
            EditSelectionCommand(operation="to_shrub")
        ]


class TestAPronounAfterAQuestionMeansTheGroupAskedAbout:
    """«Сколько кустов? Удали их» deleted the current selection, though «их»
    are the shrubs just counted -- now the same two steps as «удали все кусты»."""

    @pytest.mark.parametrize(
        "message, command, said",
        [
            ("сколько кустов? удали их", SelectCommand(target="shrubs"), "скажите «удали выделенные»"),
            ("сколько лип? сделай их кустами", SelectCommand(target="trees", species="Липа мелколистная"), "«сделай их кустами»"),
            ("какие породы деревьев? удали их", SelectCommand(target="trees"), "скажите «удали выделенные»"),
            ("сколько всего посадок? удали их", SelectCommand(target="all"), "скажите «удали выделенные»"),
            ("сколько нарушений? удали их", SelectCommand(target="violations"), "скажите «удали выделенные»"),
        ],
    )
    def test_select_and_confirm(self, message, command, said):
        reply, actions = ask(message, selection=THREE)
        assert actions == [command] and said in reply

    def test_the_violation_count_does_not_offer_what_is_already_done(self):
        reply, _ = ask("сколько нарушений? удали их", selection=THREE)
        assert reply == "Выделила 2 нарушения — скажите «удали выделенные», если хотите удалить."

    @pytest.mark.parametrize("message", ["сколько кустов? удали выделенные", "сколько выделено? удали их"])
    def test_a_question_alongside_never_deletes(self, message):
        # Strict gate (edit_gate): a message with a question in it gets answers
        # and the words to say, never a delete -- one more message is cheap.
        reply, actions = ask(message, selection=THREE)
        assert not any(isinstance(a, EditSelectionCommand) for a in actions)
        assert "«удали выделенные»" in reply


class TestQuestionsAreNotRequests:
    """The parser strips «?» with the punctuation, so «можно удалить
    выделенные?» deleted. A question or a condition gets the consequences and
    the words to say -- never an executed edit."""

    @pytest.mark.parametrize(
        "message",
        ["можно удалить выделенные?", "что будет, если удалить выделенные?", "а если удалить их?", "удали выделенные?",
         "можно удалить выделенные", "а если их удалить", "стоит ли их удалить", "можешь удалить выделенное?",
         "удали их, если можно", "Юна, а можно их удалить?"],
    )
    def test_delete_questions(self, message):
        reply, actions = ask(message, selection=THREE)
        assert actions == []
        assert reply == (
            "Если удалить выделенное (3 объекта), оно исчезнет из плана — вернуть можно Ctrl+Z. "
            "Чтобы удалить, скажите «удали выделенные»."
        )

    @pytest.mark.parametrize(
        "message, operation, said",
        [("что будет, если сделать их деревьями?", "to_tree", "могут появиться нарушения"),
         ("а если сделать их кустами?", "to_shrub", "по нормам для кустарника")],
    )
    def test_retype_questions(self, message, operation, said):
        reply, actions = ask(message, selection=THREE)
        assert actions == [] and said in reply and f"скажите «{'сделай их деревьями' if operation == 'to_tree' else 'сделай их кустами'}»" in reply

    def test_a_question_about_a_lawn_still_gets_the_lawn_answer(self):
        reply, _ = ask("можно сделать их деревьями?", selection=AssistantSelection(count=2, lawns=2))
        assert "Газон — это площадь" in reply

    def test_a_request_next_to_a_question_is_answered_not_done(self):
        reply, actions = ask("удали выделенные. А сколько кустов?", selection=THREE)
        assert actions == [] and "Кустов в плане 120" in reply
        assert ask("можно их удалить? удали", selection=THREE)[1] == []

    def test_questions_about_other_things_still_work(self):
        assert recipe(ask("можно деревья в асфальте?")[1]).trees_in_paving is True
        assert ask("можно выделить все кусты?")[1] == [SelectCommand(target="shrubs")]


class TestDeselectAndEditTogether:
    @pytest.mark.parametrize("message", ["сними выделение и удали", "удали выделенные и сними выделение"])
    def test_neither_happens_and_the_question_is_put(self, message):
        reply, actions = ask(message, selection=THREE)
        assert actions == []
        assert reply == (
            "Не поняла: удалить выделенное или только снять выделение? Выделено 3 объекта — "
            "скажите «удали выделенные» или «сними выделение»."
        )


class TestTypoCorrectionIsNeverDestructive:
    def test_no_correction_target_is_a_destructive_verb(self):
        from backend.app.services.assistant_rules import _DELETE, _ERASE, _TYPO_TARGETS

        assert not [t for t in _TYPO_TARGETS if _DELETE.search(t) or _ERASE.search(t)]

    @pytest.mark.parametrize(
        "word",
        ["удачи", "удача", "удачу", "удалы", "удала", "удалой", "удила", "удалишь", "удаляя", "удары", "удавы", "убери",
         "уберу", "убеги", "убеди", "убили", "убрал", "сотри", "стерли", "снесли", "вырежу", "удаль", "удалю"],
    )
    def test_words_one_edit_from_a_destructive_verb_stay_what_they_are(self, word):
        assert normalize(word) == word
        assert normalize(f"спасибо, {word}!") == f"спасибо, {word}!"

    @pytest.mark.parametrize("message", ["удоли выделенные", "удалт выделенные", "удачи выделенным", "уддали их"])
    def test_a_misspelt_delete_does_not_delete(self, message):
        assert not any(isinstance(a, EditSelectionCommand) for a in ask(message, selection=THREE)[1])

    @pytest.mark.parametrize(
        "message",
        ["сажай ближе к трубам", "уменьши отступ от дороги", "можно сажать прямо на кабелях?", "отключи нормы",
         "игнорируй нормы отступов", "посади деревья вплотную к зданию"],
    )
    def test_setback_norms_cannot_be_relaxed(self, message):
        reply, actions = ask(message)
        assert actions == [] and reply == NORMS_REFUSAL


# --- Interface ---------------------------------------------------------------


class TestUi:
    @pytest.mark.parametrize(
        "message, command",
        [
            ("скачай DXF", "export_dxf"),
            ("экспорт чертежа", "export_dxf"),
            ("выгрузи в автокад", "export_dxf"),
            ("отчёт CSV", "report_csv"),
            ("дай таблицу для экселя", "report_csv"),
            ("скачай отчёт", "report_json"),
            ("обоснование посадок в json", "report_json"),
            ("покажи в 3D", "show_3d"),
            ("3д", "show_3d"),
            ("покажи в объёме", "show_3d"),
        ],
    )
    def test_ui_commands(self, message, command):
        _, actions = ask(message)
        assert actions == [UiCommand(command=command)]

    @pytest.mark.parametrize("message", ["вернись на карту", "2D", "назад", "двухмерный вид"])
    def test_back_to_2d_from_3d(self, message):
        _, actions = ask(message, view="3d")
        assert actions == [UiCommand(command="show_2d")]

    def test_switching_to_the_view_already_shown_does_nothing(self):
        assert ask("покажи в 3D", view="3d") == ("План и так показан в 3D.", [])
        assert ask("вернись на карту", view="2d") == ("Мы и так на 2D-карте.", [])

    @pytest.mark.parametrize("message", ["убери 3D", "закрой 3д", "выйди из 3D", "хватит 3D", "выключи 3d вид"])
    def test_leaving_3d_goes_back_to_the_map(self, message):
        # «Убери 3D» used to open 3D (or say it was already open).
        assert ask(message, view="3d")[1] == [UiCommand(command="show_2d")]
        assert ask(message, view="2d") == ("Мы и так на 2D-карте.", [])

    def test_export_without_a_plan(self):
        reply, actions = ask("скачай DXF", facts=None)
        assert actions == [] and "Плана пока нет" in reply


def test_every_answer_fits_the_response_contract():
    """Whatever the parser produces must serialise as AssistantMessageResponse
    -- the same shape the route returns."""
    for message in ["кусты пореже и двумя рядами", "выдели липы", "удали выделенные", "скачай DXF", "почему мало деревьев?"]:
        reply, actions = ask(message, selection=THREE)
        AssistantMessageResponse.model_validate({"reply": reply, "actions": [a.model_dump() for a in actions], "mode": "rules"})


class TestLookalikes:
    """Words that look like a request but aren't -- each was a real misparse."""

    def test_show_plan_in_3d_is_the_view_not_the_summary(self):
        assert ask("покажи план в 3д")[1] == [UiCommand(command="show_3d")]

    def test_lawn_is_not_a_gas_pipe(self):
        reply, actions = ask("посади кусты ближе к газону")
        assert reply != NORMS_REFUSAL and recipe(actions).shrub_spacing_m == 0.35

    def test_gas_pipe_still_is(self):
        assert ask("посади деревья ближе к газопроводу")[0] == NORMS_REFUSAL

    def test_normalno_is_not_norms(self):
        assert ask("сделай нормально")[0] == FALLBACK_TEXT

    def test_buildings_noun_is_not_build_the_plan(self):
        _, actions = ask("посади деревья возле построек")
        assert actions == []

    def test_asking_which_model_does_not_switch_the_mode(self):
        assert ask("какая модель у тебя?")[1] == []

    def test_rows_imply_trees_for_an_interval_in_the_same_clause(self):
        settings = recipe(ask("двумя рядами через 7 м")[1])
        assert settings.tree_rows == 2 and settings.tree_spacing_m == 7.0

    def test_delete_violations_is_select_and_confirm(self):
        reply, actions = ask("удали нарушения", selection=THREE)
        assert actions == [SelectCommand(target="violations")]
        assert "скажите «удали выделенные»" in reply


def test_word_salad_never_raises_and_always_fits_the_contract():
    import random

    words = (
        "почему мало деревьев кусты газон пореже гуще интервал 6 м через 2,5 метра двумя рядами в один ряд тротуар "
        "без только добавь убери удали выделенные их это сделай деревьями в кусты выдели все липы спиреи дубы сними "
        "выделение покажи нарушения сколько пород скачай dxf отчёт csv json 3д 2d назад режим ml эвристика "
        "сгенерируй заново привет спасибо ближе к трубам отключи нормы и а , . ? ! « » — 0 15 100"
    ).split()
    rng = random.Random(2026)
    for _ in range(600):
        message = " ".join(rng.choice(words) for _ in range(rng.randint(1, 12)))
        for selection in (NOTHING, THREE, None):
            reply, actions = ask(message, selection=selection, violations=rng.choice([None, 0, 3]), view=rng.choice(["2d", "3d"]))
            assert reply
            AssistantMessageResponse.model_validate({"reply": reply, "actions": [a.model_dump() for a in actions], "mode": "rules"})
            _assert_safety_rules(message, selection, actions)


# --- Stress-test regressions ------------------------------------------------------
# Each case below was a real misparse found by running ~390 realistic messages
# (typos, colloquialisms, past tense, combined requests) through the parser.


class TestNegatedEditsNeverEdit:
    """«Не удаляй» used to delete: the delete verb matched, the «не» didn't count."""

    @pytest.mark.parametrize(
        "message", ["не удаляй выделенные", "не делай их деревьями", "не надо их удалять", "ничего не удаляй", "не трогай их"]
    )
    def test_negated_edit_is_declined(self, message):
        reply, actions = ask(message, selection=THREE)
        assert actions == [] and reply == DECLINED_TEXT

    def test_any_negation_stops_every_edit(self):
        assert ask("не удаляй, а сделай их кустами", selection=THREE)[1] == []

    def test_keep_as_is_is_declined_too(self):
        assert ask("оставь кусты как есть") == (DECLINED_TEXT, [])


class TestUnknownObjectDoesNotDeleteTheSelection:
    """«Убери подсветку» deleted the three selected objects: any «убери X» with an
    unknown X fell through to «delete what is selected»."""

    def test_remove_the_highlight_is_deselect(self):
        assert ask("убери подсветку", selection=THREE)[1] == [SelectCommand(target="none")]

    def test_unknown_object_is_not_deleted_by_guess(self):
        _, actions = ask("удали лишнее", selection=THREE)
        assert not any(isinstance(a, EditSelectionCommand) for a in actions)

    @pytest.mark.parametrize("message", ["удали пожалуйста", "ну удаляй", "можешь удалить выделенное"])
    def test_a_bare_delete_still_deletes_the_selection(self, message):
        # «Можешь удалить выделенное?» -- with the question mark -- is a
        # question now (TestQuestionsAreNotRequests).
        assert ask(message, selection=THREE)[1] == [EditSelectionCommand(operation="delete")]


class TestEditsAboutViolations:
    def test_zero_violations_never_deletes_the_selection(self):
        # Used to answer «нарушений нет» and then delete the three selected objects.
        reply, actions = ask("удали нарушения", selection=THREE, violations=0)
        assert actions == [] and reply.startswith("Нарушений нет")

    @pytest.mark.parametrize("message", ["удали деревья с нарушениями", "удали деревья, у которых нарушения"])
    def test_delete_violating_items_selects_them_and_asks(self, message):
        reply, actions = ask(message, selection=THREE, violations=3)
        assert actions == [SelectCommand(target="violations")]
        assert "скажите «удали выделенные»" in reply

    def test_retype_violating_items_selects_them_and_asks(self):
        reply, actions = ask("сделай деревья с нарушениями кустами", violations=3)
        assert actions == [SelectCommand(target="violations")] and "скажите «сделай их кустами»" in reply


class TestPastAndFutureTenseDescribeNotAsk:
    def test_i_deleted_shrubs_then_regenerate_keeps_the_shrubs(self):
        # Used to remove every shrub from the recipe.
        assert recipe(ask("я удалил лишние кусты вручную, теперь пересчитай план")[1]).planting_types == [
            "tree", "shrub", "lawn",
        ]

    def test_i_selected_trees_delete_them_keeps_my_selection(self):
        # Used to replace the user's own selection with all trees.
        trees_only = AssistantSelection(count=2, trees=2)
        assert ask("я выделил деревья, удали их", selection=trees_only)[1] == [EditSelectionCommand(operation="delete")]
        # The selection holds shrubs too -- deleting "them" would take the shrubs.
        assert ask("я выделил деревья, удали их", selection=THREE)[1] == []

    @pytest.mark.parametrize("message", ["я превратил их в кусты", "это удалит выделенные?", "ты удалила газон?"])
    def test_no_edit_for_a_description_or_a_question(self, message):
        _, actions = ask(message, selection=THREE)
        assert actions == []


class TestSelectionByPlace:
    """Rules mode can't select by place; acting on the whole group instead (all
    shrubs off the recipe for «у дороги») is worse than saying so."""

    @pytest.mark.parametrize(
        "message, verb",
        [("удали все кусты у дороги", "Удалять"), ("выдели деревья вдоль дороги", "Выделять"),
         ("сделай кусты у забора деревьями", "Менять тип")],
    )
    def test_place_gets_the_frame_selection_hint(self, message, verb):
        reply, actions = ask(message)
        assert actions == [] and reply.startswith(f"{verb} по месту") and "рамкой" in reply

    def test_u_nas_is_not_a_place(self):
        assert ask("где у нас липы")[1] == [SelectCommand(target="trees", species="Липа мелколистная")]


class TestSpacingPhrasings:
    @pytest.mark.parametrize("selection", [NOTHING, THREE])
    def test_between_shrubs_is_an_interval_not_a_retype(self, selection):
        assert recipe(ask("между кустами пусть будет 2 метра", selection=selection)[1]).shrub_spacing_m == 2.0
        assert recipe(ask("сделай расстояние между кустами 1,2 м", selection=selection)[1]).shrub_spacing_m == 1.2

    def test_a_bare_number_after_a_spacing_request_is_its_own_value(self):
        # Used to give the shrubs the trees' 6 m.
        settings = recipe(ask("интервал деревьев 6 метров, кустов 2")[1])
        assert (settings.tree_spacing_m, settings.shrub_spacing_m) == (6.0, 2.0)
        assert recipe(ask("сделай деревья реже, 8 метров")[1]).tree_spacing_m == 8.0

    def test_one_request_in_two_parts_changes_the_interval_once(self):
        settings = recipe(ask("слишком густо, разреди")[1])
        assert (settings.tree_spacing_m, settings.shrub_spacing_m) == (7.0, 0.7)

    @pytest.mark.parametrize(
        "message", ["кустов многовато", "кусты слишком плотные", "столько кустов не нужно", "уменьши количество кустов"]
    )
    def test_too_many_shrubs_means_sparser_not_fewer_types(self, message):
        settings = recipe(ask(message)[1])
        assert settings.shrub_spacing_m == 0.7 and settings.planting_types == ["tree", "shrub", "lawn"]

    def test_more_trees_by_count_is_the_norm_answer(self):
        reply, actions = ask("увеличь количество деревьев")
        assert actions == [] and reply.startswith("Гуще нормы деревья не поставить")

    def test_more_trees_switched_off_switches_them_on(self):
        settings = recipe(ask("больше деревьев", settings=_settings(planting_types=["shrub", "lawn"]))[1])
        assert settings.planting_types == ["tree", "shrub", "lawn"] and settings.tree_spacing_m is None


class TestTypos:
    @pytest.mark.parametrize(
        "message, field, value", [("кусиы пореже", "shrub_spacing_m", 0.7), ("дерьвья пореже", "tree_spacing_m", 7.0),
                                  ("пореж кусты", "shrub_spacing_m", 0.7)],
    )
    def test_a_typo_in_the_subject_changes_only_that_type(self, message, field, value):
        # Without the fix the subject wasn't found and both types changed.
        settings = recipe(ask(message)[1])
        other = "tree_spacing_m" if field == "shrub_spacing_m" else "shrub_spacing_m"
        assert getattr(settings, field) == value and getattr(settings, other) is None

    def test_typos_in_select_count_and_types_but_never_in_delete(self):
        assert ask("выдели кусиы")[1] == [SelectCommand(target="shrubs")]
        assert recipe(ask("газно убери")[1]).planting_types == ["tree", "shrub"]
        assert ask("сколко кустов")[0].startswith("Кустов в плане 120")
        # Deleting needs the verb spelt right: no correction may produce one.
        assert ask("удоли выделенные", selection=THREE)[1] == []

    @pytest.mark.parametrize(
        "word", ["деревня", "газом", "газовой", "столько", "почище", "дешево", "плотные", "удалил", "выделил", "куска"]
    )
    def test_real_words_one_letter_away_are_left_alone(self, word):
        assert normalize(word) == word


class TestQuestionsAboutNormsAndArea:
    def test_violations_of_norms_is_a_question_not_a_request_to_violate(self):
        assert ask("есть нарушения норм?", violations=0)[0].startswith("Нарушений нет")
        assert ask("покажи где нарушены нормы", violations=3)[1] == [SelectCommand(target="violations")]

    @pytest.mark.parametrize("message", ["проверь план на нормы", "все ли по нормам?", "соблюдены ли отступы?"])
    def test_norms_check_reports_the_violation_count(self, message):
        reply, actions = ask(message, violations=2)
        assert actions == [] and reply.startswith("В плане 2 нарушения")

    def test_is_this_one_compliant_explains_the_selected_item(self):
        reply, _ = ask("это дерево точно по нормам?", facts=_facts(selected=TestWhyHere.COMPLIANT),
                       selection=AssistantSelection(count=1, trees=1))
        assert reply.startswith("Липа мелколистная (дерево) стоит по нормам.")

    def test_a_trailing_why_does_not_widen_the_question(self):
        reply, _ = ask("мало деревьев, почему?")
        assert "В плане 40 деревьев" in reply and "кустов" not in reply

    def test_room_for_trees_is_the_budget_without_the_advice(self):
        reply, _ = ask("сколько места под деревья осталось")
        assert reply.startswith("В плане 40 деревьев: под деревья по нормам остаётся 204 м² из 60 701 м²")
        assert "норматив" not in reply and "ML" not in reply

    def test_lawn_area(self):
        assert ask("какая площадь газона")[0] == "Газона в плане 12 участков — 3 400 м²."

    @pytest.mark.parametrize("message", ["какое количество деревьев в плане", "кол-во деревьев", "скока деревьев"])
    def test_count_synonyms(self, message):
        assert ask(message)[0].startswith("Деревьев в плане 40")


class TestModeViewAndTypesPhrasings:
    def test_3d_model_is_the_view_not_ml_mode(self):
        assert ask("покажи 3d модель")[1] == [UiCommand(command="show_3d")]

    def test_mode_off_with_rezhim_in_between(self):
        assert recipe(ask("выключи режим ML", settings=_settings(scoring_mode="ml"))[1]).scoring_mode == "heuristic"

    def test_one_mode_request_in_two_parts_is_not_already_set(self):
        reply, actions = ask("давай без нейросети, обычный режим", settings=_settings(scoring_mode="ml"))
        assert recipe(actions).scoring_mode == "heuristic" and "Уже" not in reply

    def test_ai_mode_needs_the_word_mode(self):
        assert recipe(ask("режим ИИ")[1]).scoring_mode == "ml"
        assert ask("ты ИИ?")[1] == []

    def test_undo_points_at_ctrl_z(self):
        assert ask("отмени последнее действие") == (UNDO_TEXT, [])

    @pytest.mark.parametrize("message", ["regenerate plan", "ещё вариант"])
    def test_regenerate_phrasings(self, message):
        assert recipe(ask(message)[1]) == recipe([SettingsCommand(settings=_settings().model_dump())])

    def test_setting_a_setback_to_a_number_is_refused(self):
        assert ask("сделай отступ от дороги 1 метр") == (NORMS_REFUSAL, [])

    def test_plant_everything_then_a_list(self):
        settings = recipe(ask("посади всё: деревья, кусты и газон", settings=_settings(planting_types=["tree"]))[1])
        assert settings.planting_types == ["tree", "shrub", "lawn"]

    def test_ambiguous_keep_asks_instead_of_removing_types(self):
        reply, actions = ask("оставь деревья и газон")
        assert actions == [] and "Скажите «только деревья и газон»" in reply


def test_word_salad_with_negations_typos_and_places_keeps_the_safety_rules():
    import random

    words = (
        "не удаляй не делай не трогай удалил выделил удоли удали убери подсветку лишнее их это выделенные кусиы дерьвья "
        "газно у дороги вдоль возле дома где у нас нарушения нарушены нормы которых сделай деревьями в кусты выдели все "
        "оставь как есть столько не нужно слишком много между кустами пусть будет 2 метра , . ? « » 0 15 100 3d модель"
    ).split()
    rng = random.Random(1818)
    for _ in range(600):
        message = " ".join(rng.choice(words) for _ in range(rng.randint(1, 12)))
        for selection in (NOTHING, THREE, None):
            reply, actions = ask(message, selection=selection, violations=rng.choice([None, 0, 3]))
            assert reply
            AssistantMessageResponse.model_validate({"reply": reply, "actions": [a.model_dump() for a in actions], "mode": "rules"})
            _assert_safety_rules(message, selection, actions)
            for action in actions:
                if isinstance(action, SettingsCommand):
                    for planting_type, (low, high) in SPACING_LIMITS.items():
                        value = getattr(action.settings, f"{planting_type}_spacing_m")
                        assert value is None or low <= value <= high
                    # A tree floor at or below the narrow-crown norm is stored as «по норме».
                    assert action.settings.tree_spacing_m is None or action.settings.tree_spacing_m > 3.0


def _assert_safety_rules(message, selection, actions):
    import re

    edits = [a for a in actions if isinstance(a, EditSelectionCommand)]
    assert not edits or (selection is not None and selection.count > 0)  # rule (a)
    if any(isinstance(a, SelectCommand) for a in actions):
        assert not edits  # rule (b): no edit at all -- delete or retype -- next to a select
    sentences = [s for s in re.split(r"[.!?\n]", message) if s.strip()]
    if len(sentences) == 1 and message.rstrip().endswith("?"):
        assert not edits  # rule (c): a one-sentence question never edits


class TestStrictEditGate:
    """Holes the adversarial check found (PR #32): negation with typos, «кроме»,
    questions without «?», a request about part of the selection."""

    @pytest.mark.parametrize(
        "message",
        [
            "ни удаляй выделенные",
            "не удоляй выделеные",
            "нельзя удалять выделенные",
            "удали выделенные, хотя нет",
            "подожди, удали выделенные",
        ],
    )
    def test_negation_never_deletes(self, message):
        assert ask(message, selection=THREE)[1] == []

    @pytest.mark.parametrize("message", ["удали всё кроме выделенных", "удали остальные", "удали невыделенные"])
    def test_except_never_edits(self, message):
        reply, actions = ask(message, selection=THREE)
        assert not any(isinstance(a, EditSelectionCommand) for a in actions)

    @pytest.mark.parametrize(
        "message",
        ["а можно удалить выделенные", "удалить ли выделенные", "если удалить выделенные что будет", "как удалить выделенные"],
    )
    def test_a_question_without_a_question_mark_never_deletes(self, message):
        assert not any(isinstance(a, EditSelectionCommand) for a in ask(message, selection=THREE)[1])

    def test_a_request_about_part_of_the_selection_is_refused(self):
        reply, actions = ask("удали выделенные кусты", selection=THREE)
        assert actions == []
        assert "выделите только нужные" in reply

    def test_a_request_about_all_of_the_selection_goes_through(self):
        shrubs_only = AssistantSelection(count=2, shrubs=2)
        assert ask("удали выделенные кусты", selection=shrubs_only)[1] == [EditSelectionCommand(operation="delete")]
        assert ask("удали выделенные", selection=THREE)[1] == [EditSelectionCommand(operation="delete")]
        assert ask("можешь удалить выделенное", selection=THREE)[1] == [EditSelectionCommand(operation="delete")]

    def test_retype_of_part_of_the_selection_is_refused(self):
        assert ask("сделай выделенные кусты деревьями", selection=THREE)[1] == []
        shrubs_only = AssistantSelection(count=2, shrubs=2)
        assert ask("сделай выделенные кусты деревьями", selection=shrubs_only)[1] == [EditSelectionCommand(operation="to_tree")]
