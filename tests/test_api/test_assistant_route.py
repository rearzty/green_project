"""POST /api/projects/{id}/assistant/message and GET /api/config/assistant
end to end through FastAPI -- which mode answers when (no model -> rules,
model down -> rules with a notice, model up -> llm), and that the plan facts
and the map state from the request actually reach the answer.

The project lives in the in-memory store (backend/app/db/session.py), so no
server is needed; the model's HTTP call is mocked like in
test_assistant_service.py.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from shapely.geometry import Point

import backend.app.db.session as store
from backend.app.api import routes_assistant
from backend.app.core.config import GROQ_MODEL, SELECTEL_BASE_URL, SELECTEL_MODEL, Settings, settings
from backend.app.db.models import Plan, PlantingItemRow, Project
from backend.app.main import app
from backend.app.services import assistant_service
from backend.app.services.assistant_service import AssistantUpstreamError

SETTINGS = {"planting_types": ["tree", "shrub", "lawn"], "scoring_mode": "heuristic", "tree_rows": 1, "trees_in_paving": False}


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def no_llm(monkeypatch):
    monkeypatch.setattr(settings, "llm_base_url", "https://api.groq.com/openai/v1")
    monkeypatch.setattr(settings, "llm_api_key", None)
    monkeypatch.setattr(settings, "groq_api_key", None)


@pytest.fixture
def with_llm(monkeypatch):
    monkeypatch.setattr(settings, "llm_base_url", "https://api.groq.com/openai/v1")
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "groq_api_key", None)


@pytest.fixture
def project():
    project = Project(name="Улица")
    plan = Plan(project_id=project.id, planting_types=["tree", "shrub"], item_count=3)
    plan.items = [
        PlantingItemRow(plan_id=plan.id, geometry=Point(0, 0), planting_type="tree", species="Липа мелколистная"),
        PlantingItemRow(plan_id=plan.id, geometry=Point(9, 0), planting_type="tree", species="Липа мелколистная"),
        PlantingItemRow(plan_id=plan.id, geometry=Point(5, 5), planting_type="shrub", species="Спирея японская"),
    ]
    plan.area_budget = [{
        "planting_type": "tree", "territory_m2": 30270.0, "usable_m2": 40.0, "usable_share": 0.0013,
        "blockers": [{"object_type": "road", "label": "край проезжей части", "share": 0.89}],
    }]
    project.plans = [plan]
    store.save_project(project)
    yield project
    store._projects.pop(project.id, None)


def _post(client, project, message, **extra):
    body = {"message": message, "history": [], "settings": SETTINGS, "plan_id": project.plans[0].id, **extra}
    return client.post(f"/api/projects/{project.id}/assistant/message", json=body)


class TestWithoutAModel:
    def test_answers_in_rules_mode_instead_of_503(self, client, project, no_llm):
        response = _post(client, project, "кусты пореже")

        assert response.status_code == 200
        body = response.json()
        assert body["mode"] == "rules" and body["notice"] is None
        assert body["reply"] == "Интервал кустов в живой изгороди 0,5 → 0,7 м, перестраиваю план."
        assert body["actions"] == [{"type": "settings", "settings": {**SETTINGS, "tree_spacing_m": None, "shrub_spacing_m": 0.7}}]
        # The legacy field still carries the recipe of the first settings command.
        assert body["action"] == body["actions"][0]["settings"]

    def test_plan_facts_reach_the_answer(self, client, project, no_llm):
        body = _post(client, project, "Почему так мало деревьев?").json()

        assert "В плане 2 дерева" in body["reply"] and "40 м² из 30 270 м²" in body["reply"]
        assert "край проезжей части — 89 %" in body["reply"]
        assert body["actions"] == [] and body["action"] is None

    def test_selection_and_violations_from_the_request_are_used(self, client, project, no_llm):
        deleted = _post(client, project, "удали выделенные", selection={"count": 2, "trees": 2, "shrubs": 0, "lawns": 0}).json()
        refused = _post(client, project, "удали выделенные", selection={"count": 0, "trees": 0, "shrubs": 0, "lawns": 0}).json()
        violations = _post(client, project, "покажи нарушения", violations_count=1).json()

        assert deleted["actions"] == [{"type": "edit_selection", "operation": "delete"}]
        assert refused["actions"] == [] and "ничего не выделено" in refused["reply"]
        assert violations["actions"] == [{"type": "select", "target": "violations", "species": None}]

    def test_view_from_the_request_is_used(self, client, project, no_llm):
        body = _post(client, project, "вернись на карту", view="3d").json()

        assert body["actions"] == [{"type": "ui", "command": "show_2d"}]

    def test_works_without_a_plan_too(self, client, project, no_llm):
        body = client.post(
            f"/api/projects/{project.id}/assistant/message", json={"message": "сгенерируй план", "settings": SETTINGS}
        ).json()

        assert body["mode"] == "rules" and body["actions"][0]["type"] == "settings"

    def test_unknown_project_is_still_a_404(self, client, no_llm):
        response = client.post("/api/projects/no-such/assistant/message", json={"message": "привет", "settings": SETTINGS})

        assert response.status_code == 404


class TestWhenTheModelFails:
    def test_upstream_error_falls_back_to_rules_with_a_notice(self, client, project, with_llm, monkeypatch):
        async def failing(*args, **kwargs):
            raise AssistantUpstreamError("лимит", reason="исчерпан лимит запросов")

        monkeypatch.setattr(routes_assistant, "ask_yuna", failing)

        body = _post(client, project, "деревья двумя рядами").json()

        assert body["mode"] == "rules"
        assert body["notice"] == "ИИ сейчас недоступен (исчерпан лимит запросов) — ответила в базовом режиме"
        assert body["actions"][0]["settings"]["tree_rows"] == 2

    def test_network_failure_through_the_real_client_falls_back_too(self, client, project, with_llm, monkeypatch):
        import httpx

        class _Down:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            async def post(self, *args, **kwargs):
                raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(assistant_service.httpx, "AsyncClient", _Down)

        body = _post(client, project, "выдели все деревья").json()

        assert body["mode"] == "rules" and "нет связи" in body["notice"]
        assert body["actions"] == [{"type": "select", "target": "trees", "species": None}]

    def test_a_bug_in_the_llm_path_does_not_500(self, client, project, with_llm, monkeypatch):
        async def broken(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(routes_assistant, "ask_yuna", broken)

        response = _post(client, project, "скачай DXF")

        assert response.status_code == 200
        assert response.json()["mode"] == "rules" and "внутренняя ошибка" in response.json()["notice"]


class TestWithAModel:
    def test_llm_answer_with_commands(self, client, project, with_llm, monkeypatch):
        sent = {}

        class _Groq:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            async def post(self, url, **kwargs):
                sent.update(kwargs["json"])

                class _Response:
                    def raise_for_status(self):
                        pass

                    def json(self):
                        return {"choices": [{"message": {"content": "", "tool_calls": [
                            {"function": {"name": "update_generation_settings", "arguments": json.dumps({"trees_in_paving": True})}},
                            {"function": {"name": "ui_action", "arguments": json.dumps({"command": "show_3d"})}},
                        ]}}]}

                return _Response()

        monkeypatch.setattr(assistant_service.httpx, "AsyncClient", _Groq)

        body = _post(client, project, "разреши деревья в тротуаре и покажи в 3D", violations_count=0).json()

        assert body["mode"] == "llm" and body["notice"] is None
        assert [a["type"] for a in body["actions"]] == ["settings", "ui"]
        assert body["action"]["trees_in_paving"] is True
        assert body["reply"] == "Деревья в покрытии тротуара разрешены, перестраиваю план. Показываю план в 3D."
        system = sent["messages"][0]["content"]
        assert "Сводка текущего плана" in system and "Нарушений нормативов в плане нет." in system


class TestStatus:
    def test_no_model(self, client, no_llm):
        assert client.get("/api/config/assistant").json() == {"llm_configured": False, "provider": "none", "model": None}

    def test_groq_with_a_key(self, client, with_llm):
        assert client.get("/api/config/assistant").json() == {
            "llm_configured": True, "provider": "groq", "model": settings.llm_model,
        }

    def test_local_ollama_needs_no_key(self, client, no_llm, monkeypatch):
        monkeypatch.setattr(settings, "llm_base_url", "http://host.docker.internal:11434/v1")
        monkeypatch.setattr(settings, "llm_model", "qwen3:8b")

        assert client.get("/api/config/assistant").json() == {"llm_configured": True, "provider": "ollama", "model": "qwen3:8b"}

    def test_never_exposes_the_key(self, client, with_llm):
        assert "test-key" not in client.get("/api/config/assistant").text


class TestProviderSettings:
    @pytest.mark.parametrize(
        "base_url, key, configured, provider",
        [
            ("https://api.groq.com/openai/v1", None, False, "none"),
            ("https://api.groq.com/openai/v1", "k", True, "groq"),
            ("http://localhost:11434/v1", None, True, "ollama"),
            ("http://127.0.0.1:11434/v1", None, True, "ollama"),
            ("http://host.docker.internal:11434/v1", None, True, "ollama"),
            ("http://192.168.1.20:11434/v1", None, True, "ollama"),  # an Ollama on another machine of the LAN
            ("http://localhost:1234/v1", None, True, "openai-compatible"),  # LM Studio / llama.cpp
            ("https://api.selectel.ru/aig/v1", "k", True, "selectel"),
            ("https://api.selectel.ru/aig/v1", None, False, "none"),
            ("https://example.com/v1", "k", True, "openai-compatible"),
        ],
    )
    def test_configured_and_provider(self, base_url, key, configured, provider):
        config = Settings(llm_base_url=base_url, llm_api_key=key, groq_api_key=None)
        assert (config.llm_configured, config.llm_provider) == (configured, provider)

    def test_the_default_is_selectel_glm_and_needs_only_a_key(self):
        config = Settings(llm_api_key="k")
        assert (config.llm_base_url, config.llm_model) == (SELECTEL_BASE_URL, SELECTEL_MODEL)
        assert config.llm_configured and config.llm_provider == "selectel"

    def test_old_groq_key_counts_when_the_new_one_is_unset(self):
        # An infra/.env from when Groq was the default: the key alone still
        # means Groq, not "send a Groq key to Selectel".
        config = Settings(llm_api_key=None, groq_api_key="g")
        assert config.llm_key == "g" and config.llm_configured and config.llm_provider == "groq"
        assert config.llm_model == GROQ_MODEL

    def test_old_groq_key_with_an_explicit_endpoint_goes_there(self):
        config = Settings(llm_api_key=None, groq_api_key="g", llm_base_url="https://example.com/v1", llm_model="m")
        assert (config.llm_base_url, config.llm_model, config.llm_key) == ("https://example.com/v1", "m", "g")

    @pytest.mark.parametrize(
        "base_url, effort, sent",
        [
            ("https://api.selectel.ru/aig/v1", None, "low"),  # GLM thinks 5.5 s per answer by default
            ("https://api.selectel.ru/aig/v1", "medium", "medium"),
            ("https://api.selectel.ru/aig/v1", "off", None),
            ("https://api.groq.com/openai/v1", None, None),  # not every model accepts the field
            ("http://localhost:11434/v1", None, None),
            ("http://localhost:11434/v1", "high", "high"),
        ],
    )
    def test_reasoning_effort(self, base_url, effort, sent):
        config = Settings(llm_base_url=base_url, llm_api_key="k", llm_reasoning_effort=effort)
        assert config.llm_reasoning == sent

    def test_new_key_wins_over_the_old_one(self):
        assert Settings(llm_api_key="new", groq_api_key="old").llm_key == "new"

    def test_empty_env_values_keep_the_defaults(self, monkeypatch):
        # docker-compose passes unset variables through as empty strings.
        monkeypatch.setenv("GREENPROJECT_LLM_BASE_URL", "")
        monkeypatch.setenv("GREENPROJECT_LLM_MODEL", "")
        monkeypatch.setenv("GREENPROJECT_LLM_API_KEY", "")
        monkeypatch.setenv("GREENPROJECT_GROQ_API_KEY", "")
        config = Settings()
        assert (config.llm_base_url, config.llm_model) == (SELECTEL_BASE_URL, SELECTEL_MODEL)
        assert config.llm_key is None and not config.llm_configured


class TestAPrunedPlan:
    def test_is_materialized_before_the_facts_are_built(self, client, no_llm):
        """Another tab's generate prunes every other unedited plan
        (pipeline_service._prune_stale_plans); the chat used to build facts
        from the empty plan and say «деревьев в плане нет»."""
        import asyncio
        from pathlib import Path

        from backend.app.services import pipeline_service
        from backend.app.services.project_service import create_project_from_file

        path = Path(__file__).resolve().parents[2] / "data" / "synthetic" / "territory_0.geojson"
        project = asyncio.run(create_project_from_file("синтетика", path, source_crs="EPSG:32637"))
        store.save_project(project)
        try:
            shown = asyncio.run(pipeline_service.generate_plan(project, ["tree", "shrub"], "heuristic"))
            trees = sum(1 for item in shown.items if item.planting_type == "tree")
            asyncio.run(pipeline_service.generate_plan(project, ["tree"], "heuristic", tree_rows=2))
            assert not shown.materialized and trees > 0  # pruned by the second generate

            body = client.post(
                f"/api/projects/{project.id}/assistant/message",
                json={"message": "сколько деревьев?", "settings": SETTINGS, "plan_id": shown.id},
            ).json()

            assert body["reply"].startswith(f"Деревьев в плане {trees}")
            assert shown.materialized
        finally:
            store._projects.pop(project.id, None)
