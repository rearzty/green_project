"""Unit tests for Юна's text-to-settings translation (assistant_service.py) --
mocks the Groq HTTP call so this needs no network access and no PostGIS,
matching this suite's usual no-DB-needed style for pure-logic services.
"""

import asyncio
import json

import pytest

from backend.app.core.config import settings
from backend.app.schemas.assistant import AssistantGenerationSettings
from backend.app.services import assistant_service


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


def _chat_completion(*, content: str | None = None, tool_call_args: dict | None = None) -> dict:
    message: dict = {"content": content}
    if tool_call_args is not None:
        message["tool_calls"] = [{"function": {"name": "update_generation_settings", "arguments": json.dumps(tool_call_args)}}]
    return {"choices": [{"message": message}]}


CURRENT = AssistantGenerationSettings(planting_types=["tree", "shrub", "lawn"], tree_spacing_m=5.0, shrub_spacing_m=3.0)


def test_raises_when_api_key_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", None)
    with pytest.raises(assistant_service.AssistantNotConfiguredError):
        asyncio.run(assistant_service.ask_yuna("побольше деревьев", [], CURRENT))


def test_plain_text_reply_carries_no_action(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "test-key")
    payload = _chat_completion(content="Сейчас интервал деревьев 5 метров.")
    monkeypatch.setattr(assistant_service.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(payload, **kw))

    reply, action = asyncio.run(assistant_service.ask_yuna("какой сейчас интервал деревьев?", [], CURRENT))

    assert reply == "Сейчас интервал деревьев 5 метров."
    assert action is None


def test_tool_call_builds_action_and_keeps_untouched_fields(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "test-key")
    # Model only mentions tree_spacing_m -- shrub spacing and planting_types
    # should fall back to whatever was already set, not be wiped out.
    payload = _chat_completion(content=None, tool_call_args={"tree_spacing_m": 7.0})
    monkeypatch.setattr(assistant_service.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(payload, **kw))

    reply, action = asyncio.run(assistant_service.ask_yuna("посади деревья пореже", [], CURRENT))

    assert action is not None
    assert action.tree_spacing_m == 7.0
    assert action.shrub_spacing_m == CURRENT.shrub_spacing_m
    assert action.planting_types == CURRENT.planting_types
    assert reply  # a default confirmation fills in when the model gave no text alongside the tool call


def test_tool_call_arguments_are_clamped_to_valid_bounds(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "test-key")
    payload = _chat_completion(content="Готово.", tool_call_args={"tree_spacing_m": 999.0, "shrub_spacing_m": 0.001})
    monkeypatch.setattr(assistant_service.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(payload, **kw))

    _reply, action = asyncio.run(assistant_service.ask_yuna("посади деревья очень редко", [], CURRENT))

    assert action.tree_spacing_m == 15.0  # GenerateRequest's own upper bound
    assert action.shrub_spacing_m == 0.3  # GenerateRequest's own lower bound


def test_empty_planting_types_from_model_falls_back_to_current(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "test-key")
    payload = _chat_completion(content=None, tool_call_args={"planting_types": []})
    monkeypatch.setattr(assistant_service.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(payload, **kw))

    _reply, action = asyncio.run(assistant_service.ask_yuna("убери всё", [], CURRENT))

    # An empty list would leave nothing to generate -- treated as "the model
    # didn't actually specify a change" rather than taken literally.
    assert action.planting_types == CURRENT.planting_types


class TestAdversarialToolCallArguments:
    """A hallucinating or successfully-prompt-injected model can put anything
    in a tool call's arguments -- these confirm ask_yuna() degrades to "no
    change to that field" instead of ever raising (which would surface as a
    500 all the way up to the chat widget), regardless of what garbage
    reaches it. See _safe_spacing/_safe_planting_types in assistant_service.py."""

    def test_non_numeric_spacing_string_falls_back_to_current(self, monkeypatch):
        monkeypatch.setattr(settings, "groq_api_key", "test-key")
        payload = _chat_completion(content=None, tool_call_args={"tree_spacing_m": "очень часто"})
        monkeypatch.setattr(assistant_service.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(payload, **kw))

        _reply, action = asyncio.run(assistant_service.ask_yuna("побольше деревьев", [], CURRENT))

        assert action.tree_spacing_m == CURRENT.tree_spacing_m

    def test_nan_spacing_falls_back_to_current_not_left_unclamped(self, monkeypatch):
        # NaN comparisons are always False, so a naive max(low, min(high, x))
        # can let NaN slip through a clamp unchanged -- this is exactly the
        # case _safe_spacing's math.isfinite check exists for.
        monkeypatch.setattr(settings, "groq_api_key", "test-key")
        payload = _chat_completion(content=None, tool_call_args={"tree_spacing_m": float("nan")})
        monkeypatch.setattr(assistant_service.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(payload, **kw))

        _reply, action = asyncio.run(assistant_service.ask_yuna("побольше деревьев", [], CURRENT))

        assert action.tree_spacing_m == CURRENT.tree_spacing_m

    def test_infinite_spacing_falls_back_to_current(self, monkeypatch):
        monkeypatch.setattr(settings, "groq_api_key", "test-key")
        payload = _chat_completion(content=None, tool_call_args={"shrub_spacing_m": float("inf")})
        monkeypatch.setattr(assistant_service.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(payload, **kw))

        _reply, action = asyncio.run(assistant_service.ask_yuna("побольше кустов", [], CURRENT))

        assert action.shrub_spacing_m == CURRENT.shrub_spacing_m

    def test_hallucinated_planting_type_is_dropped_valid_ones_kept(self, monkeypatch):
        monkeypatch.setattr(settings, "groq_api_key", "test-key")
        payload = _chat_completion(content=None, tool_call_args={"planting_types": ["tree", "flowers", "lawn"]})
        monkeypatch.setattr(assistant_service.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(payload, **kw))

        _reply, action = asyncio.run(assistant_service.ask_yuna("оставь только деревья и газон", [], CURRENT))

        assert action.planting_types == ["tree", "lawn"]

    def test_planting_types_as_wrong_json_type_falls_back_to_current(self, monkeypatch):
        monkeypatch.setattr(settings, "groq_api_key", "test-key")
        payload = _chat_completion(content=None, tool_call_args={"planting_types": "tree"})  # a string, not a list
        monkeypatch.setattr(assistant_service.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(payload, **kw))

        _reply, action = asyncio.run(assistant_service.ask_yuna("оставь только деревья", [], CURRENT))

        assert action.planting_types == CURRENT.planting_types

    def test_malformed_tool_call_arguments_json_degrades_to_no_action(self, monkeypatch):
        monkeypatch.setattr(settings, "groq_api_key", "test-key")
        payload = _chat_completion(content="Хорошо.")
        payload["choices"][0]["message"]["tool_calls"] = [{"function": {"name": "update_generation_settings", "arguments": "{not valid json"}}]
        monkeypatch.setattr(assistant_service.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(payload, **kw))

        reply, action = asyncio.run(assistant_service.ask_yuna("побольше деревьев", [], CURRENT))

        # Doesn't raise -- degrades to "no fields changed", not a 500.
        assert action is not None
        assert action.tree_spacing_m == CURRENT.tree_spacing_m
        assert action.shrub_spacing_m == CURRENT.shrub_spacing_m
        assert action.planting_types == CURRENT.planting_types
        assert reply == "Хорошо."


class TestNewSettings:
    def test_tool_call_can_switch_rows_paving_and_mode(self, monkeypatch):
        monkeypatch.setattr(settings, "groq_api_key", "test-key")
        payload = _chat_completion(tool_call_args={"tree_rows": 2, "trees_in_paving": True, "scoring_mode": "ml"})
        monkeypatch.setattr(assistant_service.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(payload, **kw))

        _, action = asyncio.run(assistant_service.ask_yuna("две аллеи и деревья в тротуаре", [], CURRENT))

        assert action.tree_rows == 2 and action.trees_in_paving is True and action.scoring_mode == "ml"
        assert action.tree_spacing_m == 5.0

    def test_garbage_rows_paving_and_mode_fall_back_to_current(self, monkeypatch):
        monkeypatch.setattr(settings, "groq_api_key", "test-key")
        payload = _chat_completion(tool_call_args={"tree_rows": 7, "trees_in_paving": "yes", "scoring_mode": "magic"})
        monkeypatch.setattr(assistant_service.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(payload, **kw))

        _, action = asyncio.run(assistant_service.ask_yuna("что-нибудь", [], CURRENT))

        assert action.tree_rows == 1 and action.trees_in_paving is False and action.scoring_mode == "heuristic"

    def test_plan_context_is_sent_and_history_is_trimmed(self, monkeypatch):
        monkeypatch.setattr(settings, "groq_api_key", "test-key")
        sent = {}

        class _Recording(_FakeAsyncClient):
            async def post(self, *args, **kwargs):
                sent.update(kwargs["json"])
                return await super().post(*args, **kwargs)

        payload = _chat_completion(content="Под деревья остаётся 0,3 % участка.")
        monkeypatch.setattr(assistant_service.httpx, "AsyncClient", lambda **kw: _Recording(payload, **kw))
        from backend.app.schemas.assistant import AssistantChatMessage

        history = [AssistantChatMessage(role="user", content=f"m{i}") for i in range(40)]
        asyncio.run(assistant_service.ask_yuna("почему мало деревьев?", history, CURRENT, "Сводка текущего плана: X"))

        contents = [m["content"] for m in sent["messages"]]
        assert "Сводка текущего плана: X" in contents
        assert len(sent["messages"]) <= 3 + assistant_service._MAX_HISTORY_MESSAGES + 1


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
