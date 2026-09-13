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
