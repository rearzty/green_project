"""Unit tests for Юна's LLM path (assistant_service.py) -- mocks the HTTP call
so this needs no network access and no key, matching this suite's usual
no-server-needed style for pure-logic services.
"""

import asyncio
import json

import httpx
import pytest

from backend.app.core.config import settings
from backend.app.schemas.assistant import (
    AssistantChatMessage,
    AssistantGenerationSettings,
    AssistantSelection,
    EditSelectionCommand,
    SelectCommand,
    SettingsCommand,
    UiCommand,
)
from backend.app.services import assistant_service
from backend.app.services.assistant_context import PlanFacts, TypeFacts

GROQ_URL = "https://api.groq.com/openai/v1"


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, response_payload: dict, **kwargs):
        self._response_payload = response_payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, *args, **kwargs):
        return _FakeResponse(self._response_payload)


class _RecordingClient(_FakeAsyncClient):
    """Remembers what was sent -- URL, headers, body."""

    sent: dict = {}

    async def post(self, url, **kwargs):
        _RecordingClient.sent = {"url": url, **kwargs}
        return await super().post(url, **kwargs)


def _chat_completion(*, content: str | None = None, tool_call_args: dict | None = None, calls: list | None = None) -> dict:
    message: dict = {"content": content}
    if tool_call_args is not None:
        message["tool_calls"] = [{"function": {"name": "update_generation_settings", "arguments": json.dumps(tool_call_args)}}]
    if calls is not None:
        message["tool_calls"] = [
            {"function": {"name": name, "arguments": json.dumps(args) if not isinstance(args, str) else args}}
            for name, args in calls
        ]
    return {"choices": [{"message": message}]}


CURRENT = AssistantGenerationSettings(planting_types=["tree", "shrub", "lawn"], tree_spacing_m=5.0, shrub_spacing_m=3.0)
SELECTED = AssistantSelection(count=3, trees=1, shrubs=2)


@pytest.fixture
def llm(monkeypatch):
    """A configured provider with a key -- set explicitly, so a key in the
    developer's own environment can't change what these tests see."""
    monkeypatch.setattr(settings, "llm_base_url", GROQ_URL)
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "groq_api_key", None)

    def respond(payload: dict, client=_FakeAsyncClient):
        monkeypatch.setattr(assistant_service.httpx, "AsyncClient", lambda **kw: client(payload, **kw))

    return respond


def _ask(message, *, current=CURRENT, history=None, facts=None, **kwargs):
    return asyncio.run(assistant_service.ask_yuna(message, history or [], current, facts, **kwargs))


def _settings_of(actions) -> SettingsCommand:
    [command] = [a for a in actions if isinstance(a, SettingsCommand)]
    return command.settings


def test_raises_when_api_key_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "llm_base_url", GROQ_URL)
    monkeypatch.setattr(settings, "llm_api_key", None)
    monkeypatch.setattr(settings, "groq_api_key", None)
    with pytest.raises(assistant_service.AssistantNotConfiguredError):
        _ask("побольше деревьев")


def test_plain_text_reply_carries_no_action(llm):
    llm(_chat_completion(content="Сейчас интервал деревьев 5 метров."))

    reply, actions = _ask("какой сейчас интервал деревьев?")

    assert reply == "Сейчас интервал деревьев 5 метров."
    assert actions == []


def test_tool_call_builds_action_and_keeps_untouched_fields(llm):
    # Model only mentions tree_spacing_m -- shrub spacing and planting_types
    # should fall back to whatever was already set, not be wiped out.
    llm(_chat_completion(content=None, tool_call_args={"tree_spacing_m": 7.0}))

    reply, actions = _ask("посади деревья пореже")

    action = _settings_of(actions)
    assert action.tree_spacing_m == 7.0
    assert action.shrub_spacing_m == CURRENT.shrub_spacing_m
    assert action.planting_types == CURRENT.planting_types
    # Filled in when the model gave no text; a tree interval is a floor over the crown-class norm.
    assert reply == "Интервал деревьев — не меньше 7 м вместо 5 (широким кронам норма — 8 м), перестраиваю план."


def test_tool_call_arguments_are_clamped_to_valid_bounds(llm):
    llm(_chat_completion(content="Готово.", tool_call_args={"tree_spacing_m": 999.0, "shrub_spacing_m": 0.001}))

    _reply, actions = _ask("посади деревья очень редко")

    action = _settings_of(actions)
    assert action.tree_spacing_m == 15.0  # GenerateRequest's own upper bound
    assert action.shrub_spacing_m == 0.3  # GenerateRequest's own lower bound


def test_empty_planting_types_from_model_falls_back_to_current(llm):
    llm(_chat_completion(content=None, tool_call_args={"planting_types": []}))

    _reply, actions = _ask("убери всё")

    # An empty list would leave nothing to generate -- treated as "the model
    # didn't actually specify a change" rather than taken literally.
    assert _settings_of(actions).planting_types == CURRENT.planting_types


class TestAdversarialToolCallArguments:
    """A hallucinating or successfully-prompt-injected model can put anything
    in a tool call's arguments -- these confirm ask_yuna() degrades to "no
    change to that field" instead of ever raising (which would surface as a
    500 all the way up to the chat widget), regardless of what garbage
    reaches it. See _safe_spacing/_safe_planting_types in assistant_service.py."""

    def test_non_numeric_spacing_string_falls_back_to_current(self, llm):
        llm(_chat_completion(content=None, tool_call_args={"tree_spacing_m": "очень часто"}))

        _reply, actions = _ask("побольше деревьев")

        assert _settings_of(actions).tree_spacing_m == CURRENT.tree_spacing_m

    def test_nan_spacing_falls_back_to_current_not_left_unclamped(self, llm):
        # NaN comparisons are always False, so a naive max(low, min(high, x))
        # can let NaN slip through a clamp unchanged -- this is exactly the
        # case _safe_spacing's math.isfinite check exists for.
        llm(_chat_completion(content=None, tool_call_args={"tree_spacing_m": float("nan")}))

        _reply, actions = _ask("побольше деревьев")

        assert _settings_of(actions).tree_spacing_m == CURRENT.tree_spacing_m

    def test_infinite_spacing_falls_back_to_current(self, llm):
        llm(_chat_completion(content=None, tool_call_args={"shrub_spacing_m": float("inf")}))

        _reply, actions = _ask("побольше кустов")

        assert _settings_of(actions).shrub_spacing_m == CURRENT.shrub_spacing_m

    def test_hallucinated_planting_type_is_dropped_valid_ones_kept(self, llm):
        llm(_chat_completion(content=None, tool_call_args={"planting_types": ["tree", "flowers", "lawn"]}))

        _reply, actions = _ask("оставь только деревья и газон")

        assert _settings_of(actions).planting_types == ["tree", "lawn"]

    def test_planting_types_as_wrong_json_type_falls_back_to_current(self, llm):
        llm(_chat_completion(content=None, tool_call_args={"planting_types": "tree"}))  # a string, not a list

        _reply, actions = _ask("оставь только деревья")

        assert _settings_of(actions).planting_types == CURRENT.planting_types

    def test_malformed_tool_call_arguments_json_degrades_to_no_action(self, llm):
        payload = _chat_completion(content="Хорошо.")
        payload["choices"][0]["message"]["tool_calls"] = [
            {"function": {"name": "update_generation_settings", "arguments": "{not valid json"}}
        ]
        llm(payload)

        reply, actions = _ask("побольше деревьев")

        # Doesn't raise -- degrades to "no fields changed", not a 500.
        action = _settings_of(actions)
        assert action.tree_spacing_m == CURRENT.tree_spacing_m
        assert action.shrub_spacing_m == CURRENT.shrub_spacing_m
        assert action.planting_types == CURRENT.planting_types
        assert reply == "Хорошо."

    def test_arguments_sent_as_an_object_not_a_string_are_accepted(self, llm):
        payload = _chat_completion(content=None)
        payload["choices"][0]["message"]["tool_calls"] = [
            {"function": {"name": "update_generation_settings", "arguments": {"tree_rows": 2}}}
        ]
        llm(payload)

        _reply, actions = _ask("двумя рядами")

        assert _settings_of(actions).tree_rows == 2


class TestNewSettings:
    def test_tool_call_can_switch_rows_paving_and_mode(self, llm):
        llm(_chat_completion(tool_call_args={"tree_rows": 2, "trees_in_paving": True, "scoring_mode": "ml"}))

        _, actions = _ask("две аллеи и деревья в тротуаре")

        action = _settings_of(actions)
        assert action.tree_rows == 2 and action.trees_in_paving is True and action.scoring_mode == "ml"
        assert action.tree_spacing_m == 5.0

    def test_garbage_rows_paving_and_mode_fall_back_to_current(self, llm):
        llm(_chat_completion(tool_call_args={"tree_rows": 7, "trees_in_paving": "yes", "scoring_mode": "magic"}))

        _, actions = _ask("что-нибудь")

        action = _settings_of(actions)
        assert action.tree_rows == 1 and action.trees_in_paving is False and action.scoring_mode == "heuristic"

    def test_plan_context_is_sent_and_history_is_trimmed(self, llm):
        llm(_chat_completion(content="Под деревья остаётся 0,3 % участка."), client=_RecordingClient)
        facts = PlanFacts(plan_id="p", scoring_mode="heuristic", tree_rows=1, trees_in_paving=False, materialized=True,
                          item_count=2, counts={"tree": 2})
        history = [AssistantChatMessage(role="user", content=f"m{i}") for i in range(40)]

        _ask("почему мало деревьев?", history=history, facts=facts)

        messages = _RecordingClient.sent["json"]["messages"]
        assert "Сводка текущего плана (режим heuristic" in messages[0]["content"]
        # One system message (some chat templates reject a system message
        # anywhere but first) + the trimmed history + the question itself.
        assert [m["role"] for m in messages].count("system") == 1
        assert len(messages) == 1 + assistant_service._MAX_HISTORY_MESSAGES + 1
        assert messages[-1] == {"role": "user", "content": "почему мало деревьев?"}


class TestSeveralToolCalls:
    def test_all_calls_become_commands_in_order(self, llm):
        llm(_chat_completion(content="Готово.", calls=[
            ("update_generation_settings", {"shrub_spacing_m": 4.2}),
            ("select_items", {"target": "violations"}),
            ("ui_action", {"command": "show_3d"}),
        ]))

        _reply, actions = _ask("кусты пореже, покажи нарушения в 3D", violations_count=2, facts=PLAN)

        assert [a.type for a in actions] == ["settings", "select", "ui"]
        assert actions[1] == SelectCommand(target="violations")
        assert actions[2] == UiCommand(command="show_3d")

    def test_several_settings_calls_merge_into_one_command(self, llm):
        # The second call starts from what the first one set, not from the
        # panel's settings -- otherwise it would quietly undo the first.
        llm(_chat_completion(content=None, calls=[
            ("update_generation_settings", {"tree_spacing_m": 7.0}),
            ("ui_action", {"command": "export_dxf"}),
            ("update_generation_settings", {"tree_rows": 2}),
        ]))

        reply, actions = _ask("деревья пореже, двумя рядами и скачай DXF")

        assert [a.type for a in actions] == ["settings", "ui"]
        action = _settings_of(actions)
        assert action.tree_spacing_m == 7.0 and action.tree_rows == 2
        assert reply.startswith(
            "Интервал деревьев — не меньше 7 м вместо 5 (широким кронам норма — 8 м), деревья двумя рядами, перестраиваю план."
        )

    def test_calls_with_values_outside_the_enums_are_dropped(self, llm):
        llm(_chat_completion(content="Сделала.", calls=[
            ("edit_selection", {"operation": "explode"}),
            ("select_items", {"target": "everything"}),
            ("ui_action", {"command": "format_disk"}),
            ("drop_table", {"name": "plans"}),
            ("ui_action", {"command": "report_csv"}),
        ]))

        reply, actions = _ask("что-нибудь", selection=SELECTED)

        assert actions == [UiCommand(command="report_csv")]
        assert reply == "Сделала."

    def test_only_invalid_calls_and_no_text_is_an_upstream_error(self, llm):
        # Nothing usable came back -- the route then answers in rules mode.
        llm(_chat_completion(content=None, calls=[("ui_action", {"command": "format_disk"})]))

        with pytest.raises(assistant_service.AssistantUpstreamError) as error:
            _ask("что-нибудь")
        assert error.value.reason == "пустой ответ"

    def test_identical_calls_are_not_repeated(self, llm):
        llm(_chat_completion(content="Ок.", calls=[("ui_action", {"command": "show_3d"}), ("ui_action", {"command": "show_3d"})]))

        _reply, actions = _ask("3D")

        assert actions == [UiCommand(command="show_3d")]


class TestSafetyInCode:
    """The prompt asks for these too, but the guarantee lives in code: a
    talked-into-it model still can't make them happen."""

    def test_edit_selection_with_nothing_selected_is_dropped_and_user_asked_to_select(self, llm):
        llm(_chat_completion(content="Удаляю выделенные объекты.", calls=[("edit_selection", {"operation": "delete"})]))

        reply, actions = _ask("удали выделенные", selection=AssistantSelection(count=0))

        assert actions == []
        assert "ничего не выделено" in reply and "Удаляю" not in reply  # the model's claim is replaced, not kept

    def test_edit_selection_without_any_selection_info_is_dropped(self, llm):
        llm(_chat_completion(content=None, calls=[("edit_selection", {"operation": "to_tree"})]))

        reply, actions = _ask("сделай их деревьями", selection=None)

        assert actions == []
        assert "сделаю деревьями" in reply

    def test_delete_in_the_same_reply_as_a_select_is_dropped(self, llm):
        llm(_chat_completion(content="Удалила все кусты.", calls=[
            ("select_items", {"target": "shrubs"}),
            ("edit_selection", {"operation": "delete"}),
        ]))
        facts = PlanFacts(plan_id="p", scoring_mode="heuristic", tree_rows=1, trees_in_paving=False, materialized=True,
                          item_count=120, counts={"shrub": 120})

        reply, actions = _ask("удали все кусты", selection=SELECTED, facts=facts)

        assert actions == [SelectCommand(target="shrubs")]
        assert reply == "Выделила все кусты (120) — скажите «удали выделенные», если хотите удалить."

    def test_edit_on_an_existing_selection_goes_through(self, llm):
        llm(_chat_completion(content=None, calls=[("edit_selection", {"operation": "to_shrub"})]))

        reply, actions = _ask("сделай их кустами", selection=AssistantSelection(count=3, trees=3))

        assert actions == [EditSelectionCommand(operation="to_shrub")]
        assert reply == "Делаю выделенное кустами (3 объекта) — отменить можно Ctrl+Z."

    def test_no_tool_can_touch_setback_norms(self):
        names = {tool["function"]["name"] for tool in assistant_service._TOOLS}
        assert names == {"update_generation_settings", "edit_selection", "select_items", "ui_action"}
        settings_params = set(assistant_service._SETTINGS_TOOL_PARAMETERS["properties"])
        assert not any("setback" in p or "norm" in p or "margin" in p for p in settings_params)


class TestSpecies:
    FACTS = PlanFacts(
        plan_id="p", scoring_mode="heuristic", tree_rows=1, trees_in_paving=False, materialized=True, item_count=40,
        counts={"tree": 30, "shrub": 10},
        types={
            "tree": TypeFacts("tree", 30, species=[("Липа мелколистная", 20), ("Клён остролистный", 10)]),
            "shrub": TypeFacts("shrub", 10, species=[("Сирень обыкновенная", 10)]),
        },
    )

    def test_species_is_normalised_to_the_exact_name_in_the_plan(self, llm):
        llm(_chat_completion(content=None, calls=[("select_items", {"target": "trees", "species": "липы"})]))

        reply, actions = _ask("выдели липы", facts=self.FACTS)

        assert actions == [SelectCommand(target="trees", species="Липа мелколистная")]
        assert reply == "Выделила породу «Липа мелколистная» (20 деревьев)."

    def test_species_found_under_the_other_type_switches_the_target(self, llm):
        llm(_chat_completion(content=None, calls=[("select_items", {"target": "trees", "species": "Сирень"})]))

        _reply, actions = _ask("выдели сирень", facts=self.FACTS)

        assert actions == [SelectCommand(target="shrubs", species="Сирень обыкновенная")]

    def test_species_absent_from_the_plan_is_dropped_with_an_honest_note(self, llm):
        llm(_chat_completion(content="Выделила все дубы.", calls=[("select_items", {"target": "trees", "species": "Дуб"})]))

        reply, actions = _ask("выдели дубы", facts=self.FACTS)

        assert actions == []
        assert reply.startswith("Породы «Дуб» в плане нет") and "Липа мелколистная" in reply


class TestThinkTags:
    @pytest.mark.parametrize(
        "content",
        [
            "<think>Пользователь спрашивает про интервал.</think>Интервал деревьев 5 м.",
            "<THINK>\nдумаю\n</THINK>\n\nИнтервал деревьев 5 м.",
            "Пользователь спрашивает про интервал.</think>Интервал деревьев 5 м.",  # opening tag eaten by the template
            "Интервал деревьев 5 м.<think>а теперь ещё подумаю",  # cut off mid-thought
        ],
    )
    def test_reasoning_is_stripped_from_the_reply(self, llm, content):
        llm(_chat_completion(content=content))

        reply, _ = _ask("какой интервал?")

        assert reply == "Интервал деревьев 5 м."

    def test_reply_that_is_only_thinking_counts_as_empty(self, llm):
        llm(_chat_completion(content="<think>долго думаю…</think>"))

        with pytest.raises(assistant_service.AssistantUpstreamError):
            _ask("привет")


class TestProviderRequest:
    def test_ollama_gets_no_authorization_header(self, monkeypatch):
        monkeypatch.setattr(settings, "llm_base_url", "http://localhost:11434/v1/")
        monkeypatch.setattr(settings, "llm_model", "qwen3:8b")
        monkeypatch.setattr(settings, "llm_api_key", None)
        monkeypatch.setattr(settings, "groq_api_key", None)
        monkeypatch.setattr(
            assistant_service.httpx, "AsyncClient", lambda **kw: _RecordingClient(_chat_completion(content="Привет!"), **kw)
        )

        reply, _ = _ask("привет")

        sent = _RecordingClient.sent
        assert reply == "Привет!"
        assert sent["url"] == "http://localhost:11434/v1/chat/completions"
        assert "Authorization" not in sent["headers"]
        assert sent["json"]["model"] == "qwen3:8b" and sent["json"]["tool_choice"] == "auto"

    def test_key_goes_into_a_bearer_header(self, llm):
        llm(_chat_completion(content="Привет!"), client=_RecordingClient)

        _ask("привет")

        assert _RecordingClient.sent["headers"] == {"Authorization": "Bearer test-key"}
        assert _RecordingClient.sent["url"] == f"{GROQ_URL}/chat/completions"

    def test_selectel_gets_low_reasoning_effort_and_groq_gets_none(self, llm, monkeypatch):
        llm(_chat_completion(content="Ок."), client=_RecordingClient)
        _ask("привет")
        assert "reasoning_effort" not in _RecordingClient.sent["json"]  # GROQ_URL from the fixture

        monkeypatch.setattr(settings, "llm_base_url", "https://api.selectel.ru/aig/v1")
        _ask("привет")
        assert _RecordingClient.sent["json"]["reasoning_effort"] == "low"

    def test_old_groq_key_name_still_works(self, monkeypatch):
        monkeypatch.setattr(settings, "llm_base_url", GROQ_URL)
        monkeypatch.setattr(settings, "llm_api_key", None)
        monkeypatch.setattr(settings, "groq_api_key", "old-groq-key")
        monkeypatch.setattr(
            assistant_service.httpx, "AsyncClient", lambda **kw: _RecordingClient(_chat_completion(content="Ок."), **kw)
        )

        _ask("привет")

        assert _RecordingClient.sent["headers"] == {"Authorization": "Bearer old-groq-key"}

    def test_state_context_tells_the_model_selection_violations_and_view(self, llm):
        llm(_chat_completion(content="Ок."), client=_RecordingClient)
        facts = PlanFacts(plan_id="p", scoring_mode="heuristic", tree_rows=1, trees_in_paving=False, materialized=True, item_count=0)

        _ask("что выделено?", facts=facts, selection=SELECTED, violations_count=4, view="3d")

        system = _RecordingClient.sent["json"]["messages"][0]["content"]
        assert "Выделено на карте: 3 (деревья 1, кустарник 2, газон 0)." in system
        assert "Посадок с нарушениями нормативов: 4." in system
        assert "Вид: 3D." in system

    def test_nothing_selected_and_unchecked_plan_are_said_so(self, llm):
        llm(_chat_completion(content="Ок."), client=_RecordingClient)
        facts = PlanFacts(plan_id="p", scoring_mode="heuristic", tree_rows=1, trees_in_paving=False, materialized=True, item_count=0)

        _ask("что выделено?", facts=facts, selection=AssistantSelection(count=0), violations_count=None)

        system = _RecordingClient.sent["json"]["messages"][0]["content"]
        assert "На карте ничего не выделено" in system and "ещё не готова" in system and "Вид: 2D-карта." in system


class _ScriptedClient(_FakeAsyncClient):
    """Answers each request with the next payload of the script (an exception
    is raised instead) and remembers every request body."""

    script: list = []
    sent: list = []

    def __init__(self, **kwargs):
        pass

    async def post(self, url, **kwargs):
        _ScriptedClient.sent.append(kwargs["json"])
        answer = _ScriptedClient.script.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return _FakeResponse(answer)


PLAN = PlanFacts(
    plan_id="p", scoring_mode="heuristic", tree_rows=1, trees_in_paving=False, materialized=True, item_count=3,
    counts={"tree": 2, "shrub": 1}, types={"tree": TypeFacts("tree", 2, species=[("Липа мелколистная", 2)])},
)


class TestFollowUpTurn:
    """Asked for two things, a model often calls the first tool and stops. When
    the built-in parser finds more in the message, the model gets its calls
    answered and one more turn."""

    @pytest.fixture
    def scripted(self, llm, monkeypatch):
        _ScriptedClient.sent = []

        def run(*answers):
            _ScriptedClient.script = list(answers)
            monkeypatch.setattr(assistant_service.httpx, "AsyncClient", _ScriptedClient)

        return run

    def test_the_second_turn_adds_the_skipped_request(self, scripted):
        scripted(
            _chat_completion(content="Сначала перестрою план, потом выделю деревья.", calls=[("update_generation_settings", {"shrub_spacing_m": 4.2})]),
            _chat_completion(content=None, calls=[("select_items", {"target": "trees"})]),
        )

        reply, actions = _ask("кусты пореже, а потом выдели все деревья", facts=PLAN)

        assert [a.type for a in actions] == ["settings", "select"]
        assert _settings_of(actions).shrub_spacing_m == 4.2 and actions[1] == SelectCommand(target="trees")
        # The first turn's text promised only part of it; the reply says what happens.
        assert "потом выделю" not in reply and "перестраиваю план" in reply and "Выделила все деревья" in reply
        first, second = _ScriptedClient.sent
        assistant, tool = second["messages"][-2:]
        assert second["messages"][: len(first["messages"])] == first["messages"]
        # No ids from the server (Ollama) -- made up, and the result answers that id.
        assert assistant["role"] == "assistant" and assistant["tool_calls"][0]["id"] == "call_0"
        assert assistant["tool_calls"][0]["function"] == {"name": "update_generation_settings", "arguments": json.dumps({"shrub_spacing_m": 4.2})}
        assert tool["role"] == "tool" and tool["tool_call_id"] == "call_0" and tool["content"]

    def test_the_servers_own_call_ids_are_echoed_back(self, scripted):
        first = _chat_completion(content=None, calls=[("update_generation_settings", {"shrub_spacing_m": 4.2})])
        first["choices"][0]["message"]["tool_calls"][0]["id"] = "call_abc"
        scripted(first, _chat_completion(content=None, calls=[("select_items", {"target": "trees"})]))

        _ask("кусты пореже, а потом выдели все деревья", facts=PLAN)

        assistant, tool = _ScriptedClient.sent[1]["messages"][-2:]
        assert assistant["tool_calls"][0]["id"] == tool["tool_call_id"] == "call_abc"

    def test_no_second_turn_when_the_model_called_everything(self, scripted):
        scripted(_chat_completion(content=None, calls=[
            ("update_generation_settings", {"shrub_spacing_m": 4.2}),
            ("select_items", {"target": "trees"}),
        ]))

        _reply, actions = _ask("кусты пореже, а потом выдели все деревья", facts=PLAN)

        assert [a.type for a in actions] == ["settings", "select"] and len(_ScriptedClient.sent) == 1

    def test_no_second_turn_for_a_single_request_or_a_text_answer(self, scripted):
        scripted(
            _chat_completion(content=None, calls=[("update_generation_settings", {"shrub_spacing_m": 4.2})]),
            _chat_completion(content="Отступы — норматив, ослабить их я не могу."),
        )

        _ask("кусты пореже", facts=PLAN)
        _ask("сажай деревья ближе к трубам и выдели их", facts=PLAN)

        assert len(_ScriptedClient.sent) == 2  # one request per message

    def test_a_failed_second_turn_keeps_the_first_answer(self, scripted):
        scripted(
            _chat_completion(content="Перестраиваю план.", calls=[("update_generation_settings", {"shrub_spacing_m": 4.2})]),
            httpx.ConnectError("refused"),
        )

        reply, actions = _ask("кусты пореже, а потом выдели все деревья", facts=PLAN)

        assert [a.type for a in actions] == ["settings"] and reply == "Перестраиваю план."

    def test_a_second_turn_that_only_repeats_changes_nothing(self, scripted):
        call = ("update_generation_settings", {"shrub_spacing_m": 4.2})
        scripted(_chat_completion(content="Перестраиваю план.", calls=[call]), _chat_completion(content="Готово.", calls=[call]))

        reply, actions = _ask("кусты пореже, а потом выдели все деревья", facts=PLAN)

        assert [a.type for a in actions] == ["settings"] and reply == "Перестраиваю план."

    def test_the_safety_rules_still_apply_to_what_the_second_turn_adds(self, scripted):
        # Select and delete in one answer is never allowed, whichever turn
        # the delete came from.
        scripted(
            _chat_completion(content=None, calls=[("select_items", {"target": "trees"})]),
            _chat_completion(content=None, calls=[("edit_selection", {"operation": "delete"})]),
        )

        _reply, actions = _ask("выдели все деревья и удали выделенные", facts=PLAN, selection=SELECTED)

        assert not any(isinstance(a, EditSelectionCommand) and a.operation == "delete" for a in actions)


class TestWordsWithoutACall:
    """The model called no tool. Its text stays -- unless the built-in parser
    finds something to do in the message, or the model claims a deed the
    parser can check (seen live: "Возвращаю на 2D-карту." with no call)."""

    def test_a_claimed_view_switch_gets_the_parsers_command(self, llm):
        llm(_chat_completion(content="Возвращаю на 2D-карту."))

        reply, actions = _ask("вернись на карту", facts=PLAN, view="3d")

        assert actions == [UiCommand(command="show_2d")] and "2D" in reply

    def test_a_claimed_deletion_with_nothing_selected_becomes_the_truth(self, llm):
        llm(_chat_completion(content="Готово — выделенные кустарники удалены с карты."))

        reply, actions = _ask("удали выделенные", facts=PLAN, selection=AssistantSelection(count=0))

        assert actions == [] and "ничего не выделено" in reply and "удалены" not in reply

    def test_an_action_request_answered_in_words_gets_the_parsers_commands(self, llm):
        llm(_chat_completion(content="Конечно, сейчас всё сделаю."))

        _reply, actions = _ask("кусты пореже", facts=PLAN)

        assert _settings_of(actions).shrub_spacing_m == 4.2

    def test_an_explanation_or_a_refusal_is_kept(self, llm):
        llm(_chat_completion(content="Под деревья осталось 10 % участка, остальное закрыли отступы."))
        assert _ask("почему так мало деревьев?", facts=PLAN) == ("Под деревья осталось 10 % участка, остальное закрыли отступы.", [])

        llm(_chat_completion(content="Не могу: отступы от сетей — норматив."))
        assert _ask("сажай деревья ближе к трубам", facts=PLAN) == ("Не могу: отступы от сетей — норматив.", [])

    def test_a_claim_the_parser_cannot_check_is_left_alone(self, llm):
        # Nothing better to say: the parser doesn't understand the message either.
        llm(_chat_completion(content="Готово!"))

        assert _ask("сделай красиво", facts=PLAN) == ("Готово!", [])

    def test_a_negated_verb_is_not_a_claim(self, llm):
        llm(_chat_completion(content="Я не удалила ничего: сначала выделите объекты."))

        reply, _ = _ask("удали выделенные", facts=PLAN, selection=AssistantSelection(count=0))

        assert reply == "Я не удалила ничего: сначала выделите объекты."


class _FailingClient(_FakeAsyncClient):
    def __init__(self, exc: Exception | None = None, status: int | None = None, body: object = None, **kwargs):
        self._exc, self._status, self._body = exc, status, body

    async def post(self, url, **kwargs):
        if self._exc is not None:
            raise self._exc
        request = httpx.Request("POST", url)
        if isinstance(self._body, str):
            return httpx.Response(self._status or 200, text=self._body, request=request)
        return httpx.Response(self._status or 200, json=self._body, request=request)


class TestUpstreamErrors:
    @pytest.mark.parametrize(
        "client_kwargs, reason",
        [
            ({"status": 429, "body": {"error": "rate limit"}}, "исчерпан лимит запросов"),
            ({"status": 401, "body": {"error": "bad key"}}, "ключ не подошёл"),
            ({"status": 402, "body": {"error": "insufficient balance"}}, "закончился баланс"),
            ({"status": 404, "body": {"error": "model_not_found"}}, "модель не найдена"),
            ({"status": 503, "body": {"error": "down"}}, "ошибка 503"),
            ({"exc": httpx.ConnectError("refused")}, "нет связи"),
            ({"exc": httpx.ReadTimeout("slow")}, "не ответил вовремя"),
            ({"status": 200, "body": "<html>not json</html>"}, "непонятный ответ"),
            ({"status": 200, "body": {"unexpected": True}}, "непонятный ответ"),
            ({"status": 200, "body": {"choices": []}}, "непонятный ответ"),
        ],
    )
    def test_each_failure_becomes_an_upstream_error_with_a_short_reason(self, llm, monkeypatch, client_kwargs, reason):
        monkeypatch.setattr(assistant_service.httpx, "AsyncClient", lambda **kw: _FailingClient(**client_kwargs))

        with pytest.raises(assistant_service.AssistantUpstreamError) as error:
            _ask("привет")

        assert error.value.reason == reason
        assert str(error.value)  # a full Russian message too, for logs and direct callers


def test_plan_context_summarises_counts_schemes_and_area_budget():
    from shapely.geometry import Point

    from backend.app.db.models import Plan, PlantingItemRow, Project
    from backend.app.services.assistant_context import plan_context
    from geo_engine.planner import ROW_RATIONALE_PREFIX, TREE_GROUP_RATIONALE_PREFIX

    project = Project(name="t")
    plan = Plan(project_id=project.id, scoring_mode="heuristic", planting_types=["tree"])
    plan.items = [
        PlantingItemRow(plan_id=plan.id, geometry=Point(0, 0), planting_type="tree", species="Липа", rationale=f"{ROW_RATIONALE_PREFIX} x"),
        PlantingItemRow(plan_id=plan.id, geometry=Point(9, 0), planting_type="tree", species="Клён", rationale=f"{TREE_GROUP_RATIONALE_PREFIX} y"),
    ]
    plan.area_budget = [{
        "planting_type": "tree", "territory_m2": 60701.0, "usable_m2": 204.0, "usable_share": 0.0034,
        "blockers": [{"object_type": "road", "label": "край проезжей части", "share": 0.92}],
    }]
    project.plans = [plan]

    text = plan_context(project, plan.id)

    assert "деревья 2" in text
    assert "рядом вдоль улиц и границы участка 1" in text and "группами деревьев 1" in text
    assert "204 м² из 60701" in text and "край проезжей части — 92 %" in text
    assert plan_context(project, "нет-такого") is None


def test_plan_facts_keep_structure_for_the_rules_mode():
    from shapely.geometry import Point, box

    from backend.app.db.models import Plan, PlantingItemRow, Project
    from backend.app.services.assistant_context import build_plan_facts
    from geo_engine.planner import IN_PAVING_RATIONALE_PREFIX, ROW_RATIONALE_PREFIX

    project = Project(name="t")
    plan = Plan(project_id=project.id, planting_types=["tree", "lawn"], tree_rows=2, trees_in_paving=True)
    plan.items = [
        PlantingItemRow(plan_id=plan.id, geometry=Point(0, 0), planting_type="tree", species="Липа",
                        rationale=f"{ROW_RATIONALE_PREFIX} {IN_PAVING_RATIONALE_PREFIX}"),
        PlantingItemRow(plan_id=plan.id, geometry=Point(9, 0), planting_type="tree", species="Липа", rationale=""),
        PlantingItemRow(plan_id=plan.id, geometry=box(0, 0, 10, 5), planting_type="lawn", species="default"),
    ]
    plan.area_budget = [{"planting_type": "tree", "territory_m2": 1000.0, "usable_m2": 100.0, "usable_share": 0.1,
                         "blockers": [{"object_type": "road", "label": "край проезжей части", "share": 0.5}]}]
    project.plans = [plan]

    facts = build_plan_facts(project, plan.id)

    assert facts.counts == {"tree": 2, "lawn": 1}
    assert facts.types["tree"].schemes == [("рядом вдоль улиц и границы участка", 1)]
    assert facts.types["tree"].loose == 1 and facts.types["tree"].in_paving == 1
    assert facts.species("tree") == [("Липа", 2)]
    assert facts.lawn_area_m2 == 50.0
    assert facts.budget("tree").blockers[0].object_type == "road"
    assert facts.budget("shrub") is None
    assert facts.tree_rows == 2 and facts.trees_in_paving is True and facts.selected is None


NORM = AssistantGenerationSettings(planting_types=["tree", "shrub", "lawn"])  # no own intervals: the plan's real state
OAKLESS = PlanFacts(
    plan_id="p", scoring_mode="heuristic", tree_rows=1, trees_in_paving=False, materialized=True, item_count=40,
    counts={"tree": 30, "lawn": 10}, types={"tree": TypeFacts("tree", 30, species=[("Липа мелколистная", 30)])},
)


class TestAFailedSelectInTheSameAnswer:
    """The model's select_items was dropped (species not in the plan, nothing
    to select, a made-up target) -- its edit_selection must not fall back on
    whatever is selected right now."""

    @pytest.mark.parametrize(
        "select_args, violations, said",
        [
            ({"target": "trees", "species": "Дуб"}, 2, "Породы «Дуб» в плане нет"),
            ({"target": "violations"}, 0, "Нарушений нет — все посадки проходят по нормам."),
            ({"target": "violations"}, None, "ещё не готова"),
            ({"target": "shrubs"}, 2, "Кустов в плане нет — выделять нечего."),
            ({"target": "everything"}, 2, None),
        ],
    )
    @pytest.mark.parametrize("operation", ["delete", "to_shrub"])
    def test_the_edit_is_dropped_too(self, llm, select_args, violations, said, operation):
        llm(_chat_completion(content="Готово, удалила.", calls=[("select_items", select_args), ("edit_selection", {"operation": operation})]))

        reply, actions = _ask("выдели их и удали", facts=OAKLESS, selection=SELECTED, violations_count=violations)

        assert actions == []
        assert "удалила" not in reply
        if said:
            assert said in reply  # the refusal first, then what wasn't done
        assert reply.endswith("Удалять ничего не стала." if operation == "delete" else "Тип не меняла.")

    def test_a_successful_select_and_a_retype_is_two_steps(self, llm):
        llm(_chat_completion(content="Сделала их деревьями.", calls=[
            ("select_items", {"target": "shrubs"}), ("edit_selection", {"operation": "to_tree"}),
        ]))
        facts = PlanFacts(plan_id="p", scoring_mode="heuristic", tree_rows=1, trees_in_paving=False, materialized=True,
                          item_count=120, counts={"shrub": 120})

        reply, actions = _ask("сделай все кусты деревьями", facts=facts, selection=SELECTED)

        assert actions == [SelectCommand(target="shrubs")]
        assert reply == "Выделила все кусты (120) — скажите «сделай их деревьями», если хотите поменять тип."


class TestKnownEmptySelect:
    def test_selecting_zero_violations_is_said_truthfully(self, llm):
        llm(_chat_completion(content="Выделила посадки с нарушениями.", calls=[("select_items", {"target": "violations"})]))

        reply, actions = _ask("покажи нарушения", facts=PLAN, violations_count=0)

        assert actions == [] and reply == "Нарушений нет — все посадки проходят по нормам."

    def test_selecting_an_absent_type(self, llm):
        llm(_chat_completion(content="Выделила все кусты.", calls=[("select_items", {"target": "shrubs"})]))

        reply, actions = _ask("выдели кусты", facts=OAKLESS)

        assert actions == [] and reply == "Кустов в плане нет — выделять нечего."

    def test_violations_that_exist_are_selected(self, llm):
        llm(_chat_completion(content=None, calls=[("select_items", {"target": "violations"})]))

        reply, actions = _ask("покажи нарушения", facts=PLAN, violations_count=1)

        assert actions == [SelectCommand(target="violations")] and reply == "Выделила 1 нарушение."


class TestQuestionsInTheModelPath:
    """A model may obey «можно удалить выделенные?» literally: the code drops
    the edit and answers with what would happen."""

    @pytest.mark.parametrize("message", ["можно удалить выделенные?", "что будет, если удалить выделенные?", "а если удалить их"])
    def test_a_question_never_deletes(self, llm, message):
        llm(_chat_completion(content="Удаляю выделенные.", calls=[("edit_selection", {"operation": "delete"})]))

        reply, actions = _ask(message, facts=PLAN, selection=SELECTED)

        assert actions == []
        assert reply == (
            "Если удалить выделенное (3 объекта), оно исчезнет из плана — вернуть можно Ctrl+Z. "
            "Чтобы удалить, скажите «удали выделенные»."
        )

    def test_a_retype_question(self, llm):
        llm(_chat_completion(content=None, calls=[("edit_selection", {"operation": "to_tree"})]))

        reply, actions = _ask("что будет, если сделать их деревьями?", facts=PLAN, selection=SELECTED)

        assert actions == [] and "могут появиться нарушения" in reply and "«сделай их деревьями»" in reply

    def test_a_plain_request_next_to_a_question_goes_through(self, llm):
        llm(_chat_completion(content=None, calls=[("edit_selection", {"operation": "delete"})]))

        _reply, actions = _ask("удали выделенные. А сколько кустов?", facts=PLAN, selection=SELECTED)

        assert actions == [EditSelectionCommand(operation="delete")]


class TestDeselectAndEditInOneAnswer:
    def test_neither_runs_and_the_selection_stays(self, llm):
        llm(_chat_completion(content="Сняла выделение и удалила.", calls=[
            ("select_items", {"target": "none"}), ("edit_selection", {"operation": "delete"}),
        ]))

        reply, actions = _ask("сними выделение и удали", facts=PLAN, selection=SELECTED)

        assert actions == []
        assert reply.startswith("Не поняла: удалить выделенное или только снять выделение? Выделено 3 объекта")
        assert "ничего не выделено" not in reply


class TestIntervalSemantics:
    """A tree interval is a floor over the crown-class norm (3/5/8 m), shrubs
    are mostly a 0.5 m hedge -- what the model is told, and what the code
    holds when the model computes from the old 5 m / 3 m defaults anyway."""

    def test_the_prompt_and_the_context_tell_the_real_intervals(self, llm):
        llm(_chat_completion(content="Ок."), client=_RecordingClient)

        _ask("какой интервал?", current=NORM)

        system = _RecordingClient.sent["json"]["messages"][0]["content"]
        assert "{" not in system.split("Текущие настройки")[0]  # every placeholder filled in
        assert "широкая крона — 8 м, средняя — 5 м, узкая — 3 м" in system
        assert "интервал деревьев = не задан — по норме класса кроны" in system
        assert "интервал кустарников = не задан — живая изгородь 0.5 м" in system
        assert "поставь 7 м" in system and "шагом 0.5 м" in system

    def test_an_own_tree_floor_is_told_as_a_floor(self, llm):
        llm(_chat_completion(content="Ок."), client=_RecordingClient)

        _ask("какой интервал?", current=AssistantGenerationSettings(planting_types=["tree"], tree_spacing_m=7.0, shrub_spacing_m=0.7))

        system = _RecordingClient.sent["json"]["messages"][0]["content"]
        assert "интервал деревьев = не меньше 7.0 м" in system and "интервал кустарников = 0.7 м (шаг живой изгороди)" in system

    def test_default_spacing_is_the_crown_norm_and_the_hedge(self):
        assert assistant_service.default_spacing() == {"tree": 5.0, "shrub": 0.5}

    @pytest.mark.parametrize("tree", [3.5, 2.0, 3.0])
    def test_denser_trees_at_the_norm_change_nothing_whatever_the_model_sends(self, llm, tree):
        # 3.5 m (the old «5 m × 0.7») only thins the narrow crowns out; 3 m and
        # below moves no tree at all.
        llm(_chat_completion(content="Сделала деревья гуще.", calls=[("update_generation_settings", {"tree_spacing_m": tree})]))

        reply, actions = _ask("сделай деревья погуще", current=NORM, facts=PLAN)

        assert actions == []
        assert reply.startswith("Гуще нормы деревья не поставить") and "гуще." not in reply
        assert "«деревья двумя рядами»" in reply

    def test_the_norm_answer_keeps_the_rest_of_the_call(self, llm):
        llm(_chat_completion(content=None, calls=[("update_generation_settings", {"tree_spacing_m": 3.5, "tree_rows": 2})]))

        reply, actions = _ask("деревья погуще, двумя рядами", current=NORM, facts=PLAN)

        settings_ = _settings_of(actions)
        assert settings_.tree_spacing_m is None and settings_.tree_rows == 2
        assert "Гуще нормы деревья не поставить" in reply and reply.endswith("Деревья двумя рядами, перестраиваю план.")

    def test_a_floor_that_reaches_the_norm_is_dropped(self, llm):
        llm(_chat_completion(content=None, calls=[("update_generation_settings", {"tree_spacing_m": 2.8})]))

        reply, actions = _ask("деревья погуще", current=AssistantGenerationSettings(planting_types=["tree"], tree_spacing_m=4.0))

        assert _settings_of(actions).tree_spacing_m is None
        assert reply == "Интервал деревьев — по норме класса кроны (3–8 м), перестраиваю план."

    def test_denser_shrubs_from_the_old_3_m_default_are_corrected(self, llm):
        # 3 m × 0.7 = 2.1 m would make the 0.5 m hedge four times sparser.
        llm(_chat_completion(content="Сделала кусты гуще.", calls=[("update_generation_settings", {"shrub_spacing_m": 2.1})]))

        _reply, actions = _ask("кусты погуще", current=NORM, facts=PLAN)

        assert _settings_of(actions).shrub_spacing_m == 0.35

    def test_a_model_number_in_the_right_direction_stands(self, llm):
        llm(_chat_completion(content=None, calls=[("update_generation_settings", {"shrub_spacing_m": 0.8, "tree_spacing_m": 6.0})]))

        reply, actions = _ask("кусты и деревья пореже", current=NORM, facts=PLAN)

        settings_ = _settings_of(actions)
        assert (settings_.shrub_spacing_m, settings_.tree_spacing_m) == (0.8, 6.0)
        assert reply.startswith("Интервал деревьев — не меньше 6 м (широким кронам норма — 8 м)")


SHRUBBY = PlanFacts(
    plan_id="p", scoring_mode="heuristic", tree_rows=1, trees_in_paving=False, materialized=True, item_count=150,
    counts={"shrub": 120, "tree": 30},
    types={"tree": TypeFacts("tree", 30, species=[("Липа мелколистная", 30)]), "shrub": TypeFacts("shrub", 120, species=[("Спирея японская", 120)])},
)
CONFIRM_SHRUBS = "Выделила все кусты (120) — скажите «удали выделенные», если хотите удалить."


class TestDeleteAGroupIsOneMeaning:
    """«Удали все кусты» is select + confirm in both modes. Seen live on GLM-5.3
    Flash: the shrubs removed from the recipe instead (1 in 4), or selected
    with no word about confirming."""

    def test_the_parser_agrees_with_the_model_so_no_second_turn(self, llm, monkeypatch):
        _ScriptedClient.sent = []
        _ScriptedClient.script = [_chat_completion(content="Выделила все кустарники на карте — их 120 штук.",
                                                   calls=[("select_items", {"target": "shrubs"})])]
        monkeypatch.setattr(assistant_service.httpx, "AsyncClient", _ScriptedClient)

        reply, actions = _ask("удали все кусты", facts=SHRUBBY, selection=SELECTED)

        assert actions == [SelectCommand(target="shrubs")] and len(_ScriptedClient.sent) == 1
        assert reply == CONFIRM_SHRUBS  # the confirmation is always asked for

    @pytest.mark.parametrize(
        "calls",
        [
            [("update_generation_settings", {"planting_types": ["tree", "lawn"]})],
            [("edit_selection", {"operation": "delete"})],
            [("select_items", {"target": "shrubs"}), ("update_generation_settings", {"planting_types": ["tree", "lawn"]})],
        ],
    )
    def test_any_other_answer_gives_way_to_the_two_steps(self, llm, calls):
        llm(_chat_completion(content="Убираю кустарник из плана:", calls=calls))

        reply, actions = _ask("удали все кусты", facts=SHRUBBY, selection=SELECTED)

        assert actions == [SelectCommand(target="shrubs")] and reply == CONFIRM_SHRUBS

    def test_mass_retype_too(self, llm):
        llm(_chat_completion(content=None, calls=[("edit_selection", {"operation": "to_tree"})]))

        reply, actions = _ask("сделай все кусты деревьями", facts=SHRUBBY, selection=SELECTED)

        assert actions == [SelectCommand(target="shrubs")]
        assert reply == "Выделила все кусты (120) — скажите «сделай их деревьями», если хотите поменять тип."

    def test_the_recipe_wording_is_still_the_recipe(self, llm):
        llm(_chat_completion(content=None, calls=[("update_generation_settings", {"planting_types": ["tree", "lawn"]})]))

        reply, actions = _ask("убери кусты из плана", facts=SHRUBBY, selection=SELECTED)

        assert _settings_of(actions).planting_types == ["tree", "lawn"] and reply == "Без кустов, перестраиваю план."

    def test_a_pronoun_after_a_count_is_the_counted_group(self, llm):
        # «Их» are the shrubs just counted; the reply keeps the count too.
        llm(_chat_completion(content="Удаляю их.", calls=[("select_items", {"target": "shrubs"})]))

        reply, actions = _ask("сколько кустов? удали их", facts=SHRUBBY, selection=SELECTED)

        assert actions == [SelectCommand(target="shrubs")]
        assert reply.startswith("Кустов в плане 120.") and reply.endswith(CONFIRM_SHRUBS)

    def test_a_bare_delete_for_an_absent_species_never_hits_the_selection(self, llm):
        llm(_chat_completion(content="Удаляю дубы.", calls=[("edit_selection", {"operation": "delete"})]))

        reply, actions = _ask("удали дубы", facts=SHRUBBY, selection=SELECTED)

        assert actions == [] and reply.startswith("Породы «Дуб черешчатый» в плане нет")


class TestLeadInReplies:
    def test_a_reply_that_only_leads_in_to_the_calls_is_replaced(self, llm):
        llm(_chat_completion(content="Конечно, выделяю посадки с нарушениями нормативов:",
                             calls=[("select_items", {"target": "violations"})]))

        reply, _ = _ask("покажи нарушения", facts=PLAN, violations_count=4)

        assert reply == "Выделила 4 нарушения."

    def test_offers_at_the_tree_norm_are_offered_not_applied(self, llm):
        # Seen live 1 in 4: «погуще» answered by switching two rows and trees in
        # paving on unasked, with «включу двухрядную аллею … :».
        llm(_chat_completion(content="Гуще нельзя, зато включу двухрядную аллею и деревья в тротуаре:",
                             calls=[("update_generation_settings", {"tree_rows": 2, "trees_in_paving": True})]))

        reply, actions = _ask("сделай деревья погуще", current=NORM, facts=PLAN)

        assert actions == [] and reply.startswith("Гуще нормы деревья не поставить") and "«деревья двумя рядами»" in reply


def test_plan_facts_count_manual_edits_so_violations_are_not_blamed_on_them_blindly():
    from shapely.geometry import Point

    from backend.app.db.models import Plan, PlantingItemRow, Project
    from backend.app.services.assistant_context import build_plan_facts, render_plan_facts

    project = Project(name="t")
    plan = Plan(project_id=project.id, planting_types=["tree"])
    plan.items = [
        PlantingItemRow(plan_id=plan.id, geometry=Point(0, 0), planting_type="tree", species="Липа", is_manual_edit=True),
        PlantingItemRow(plan_id=plan.id, geometry=Point(9, 0), planting_type="tree", species="Липа"),
    ]
    project.plans = [plan]

    facts = build_plan_facts(project, plan.id)

    assert facts.manual_edits == 1
    assert "- изменено вручную (перенос, смена типа): 1" in render_plan_facts(facts)
    plan.materialized, plan.items = False, []
    assert build_plan_facts(project, plan.id).manual_edits is None  # unknown, not «none»
