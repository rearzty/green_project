"""«Остановить» и частичные результаты генерации (п. 8 редизайна).

Свойство, ради которого всё: остановленная генерация НИЧЕГО не оставляет —
ни нового плана, ни снятого признака «текущий» у прежнего. Второе — реальная
находка: `generate_plan` снимал `is_current` у прежних планов ДО расчёта, и
любая упавшая или остановленная генерация оставляла проект вовсе без текущего
плана (на нём держатся повторное использование плана и бейдж в истории).
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from shapely.geometry import LineString, box

import backend.app.db.session as store
from backend.app.db.models import Layer, Project
from backend.app.main import app
from backend.app.services import generation_jobs
from backend.app.services.pipeline_service import generate_plan
from geo_engine import planner
from geo_engine.planner import GenerationCancelled


def _project() -> Project:
    project = Project(name="cancel")
    project.layers = [
        Layer(project_id=project.id, kind="zone", object_type="territory", geometry=box(0, 0, 120, 90)),
        Layer(project_id=project.id, kind="utility", object_type="gas_pipe", geometry=LineString([(0, 45), (120, 45)])),
    ]
    return project


class TestCancelledGenerationLeavesNothing:
    def test_a_stop_before_any_work_adds_no_plan(self):
        project = _project()

        with pytest.raises(GenerationCancelled):
            asyncio.run(generate_plan(project, ["tree", "lawn"], "heuristic", should_stop=lambda: True))

        assert project.plans == []

    def test_a_stop_midway_keeps_the_previous_plan_current(self):
        project = _project()
        first = asyncio.run(generate_plan(project, ["tree"], "heuristic"))
        assert first.is_current

        stop = {"now": False}
        with pytest.raises(GenerationCancelled):
            asyncio.run(
                generate_plan(
                    project,
                    ["tree", "lawn"],
                    "heuristic",
                    # тип посчитан — и пользователь нажимает «Остановить»
                    on_items=lambda planting_type, items: stop.__setitem__("now", True),
                    should_stop=lambda: stop["now"],
                )
            )

        assert project.plans == [first]
        assert first.is_current, "прежний план остаётся текущим"

    def test_a_stop_right_before_the_plan_joins_the_project_still_wins(self):
        project = _project()
        first = asyncio.run(generate_plan(project, ["tree"], "heuristic"))
        calls = {"n": 0}

        def stop_on_the_last_check() -> bool:
            # Последняя проверка — прямо перед добавлением плана в проект.
            calls["n"] += 1
            return calls["n"] >= 8

        # Сколько проверок будет до точки невозврата, заранее не знаем; важно, что
        # при ЛЮБОМ числе плана либо нет, либо он целиком — а прежний остаётся текущим.
        try:
            asyncio.run(generate_plan(project, ["tree"], "heuristic", tree_spacing_m=7.0, should_stop=stop_on_the_last_check))
        except GenerationCancelled:
            assert project.plans == [first] and first.is_current
        else:
            assert len(project.plans) == 2 and sum(p.is_current for p in project.plans) == 1

    def test_a_failed_generation_no_longer_strips_the_current_plan(self):
        project = _project()
        first = asyncio.run(generate_plan(project, ["tree"], "heuristic"))

        def boom(*args, **kwargs):
            raise RuntimeError("упало посреди расчёта")

        import backend.app.services.pipeline_service as pipeline

        original = pipeline._compute_planting_rows
        pipeline._compute_planting_rows = boom
        try:
            with pytest.raises(RuntimeError):
                asyncio.run(generate_plan(project, ["tree"], "heuristic", tree_spacing_m=9.0))
        finally:
            pipeline._compute_planting_rows = original

        assert project.plans == [first] and first.is_current


class TestPartialResults:
    def test_each_type_is_reported_as_soon_as_it_is_done(self):
        project = _project()
        seen: dict[str, int] = {}
        progress: list[tuple[str, int, int]] = []

        plan = asyncio.run(
            generate_plan(
                project,
                ["tree", "shrub", "lawn"],
                "heuristic",
                on_items=lambda planting_type, items: seen.__setitem__(planting_type, len(items)),
                on_progress=lambda planting_type, done, total: progress.append((planting_type, done, total)),
            )
        )

        assert set(seen) == {"tree", "shrub", "lawn"}
        for planting_type, count in seen.items():
            assert count == sum(1 for item in plan.items if item.planting_type == planting_type)
        assert [done for _, done, _ in progress] == [1, 2, 3]

    def test_parallel_branch_reports_in_completion_order_and_keeps_item_order(self, monkeypatch):
        monkeypatch.setattr(planner, "_PARALLEL_PLAN_THRESHOLD", 0)
        project = _project()
        seen: list[str] = []

        plan = asyncio.run(generate_plan(project, ["tree", "lawn"], "heuristic", on_items=lambda t, items: seen.append(t)))

        assert sorted(seen) == ["lawn", "tree"]
        types_in_plan = [item.planting_type for item in plan.items]
        assert types_in_plan == sorted(types_in_plan, key=["tree", "lawn"].index), "порядок позиций — порядок planting_types"

    def test_parallel_branch_stops_and_leaves_no_workers_hanging(self, monkeypatch):
        monkeypatch.setattr(planner, "_PARALLEL_PLAN_THRESHOLD", 0)
        project = _project()

        with pytest.raises(GenerationCancelled):
            asyncio.run(generate_plan(project, ["tree", "shrub", "lawn"], "heuristic", should_stop=lambda: True))

        assert project.plans == []


class TestJobBookkeeping:
    def test_cancel_marks_the_job_at_once_and_frees_the_partial_result(self):
        job_id = generation_jobs.create_job()
        generation_jobs.add_partial(job_id, "tree", [object(), object()])
        job = generation_jobs.get_job(job_id)
        assert generation_jobs.partial_counts(job) == {"tree": 2} and job.partial_version == 1

        assert generation_jobs.request_cancel(job_id) == "cancelled"

        assert job.status == "cancelled" and generation_jobs.should_stop(job_id)
        assert generation_jobs.partial_counts(job) == {}

    def test_late_results_do_not_resurrect_a_cancelled_job(self):
        job_id = generation_jobs.create_job()
        generation_jobs.request_cancel(job_id)

        generation_jobs.add_partial(job_id, "tree", [object()])
        generation_jobs.mark_done(job_id, "some-plan")
        generation_jobs.mark_error(job_id, "поздняя ошибка")

        job = generation_jobs.get_job(job_id)
        assert job.status == "cancelled" and job.plan_id is None and job.error is None
        assert generation_jobs.partial_counts(job) == {}

    def test_a_finished_job_cannot_be_cancelled(self):
        job_id = generation_jobs.create_job()
        generation_jobs.mark_done(job_id, "plan-x")

        assert generation_jobs.request_cancel(job_id) == "done"
        assert not generation_jobs.should_stop(job_id)

    def test_unknown_job(self):
        assert generation_jobs.request_cancel("нет-такой") is None


class TestRoutes:
    def test_cancel_route(self):
        client = TestClient(app)
        job_id = generation_jobs.create_job()

        assert client.post(f"/api/projects/p/generate/{job_id}/cancel").json() == {"status": "cancelled"}
        status = client.get(f"/api/projects/p/generate/{job_id}").json()
        assert status["status"] == "cancelled" and status["partial_counts"] == {}
        assert client.post("/api/projects/p/generate/нет-такой/cancel").status_code == 404

    def test_partial_route_serves_what_is_already_placed(self):
        project = _project()
        store.save_project(project)
        client = TestClient(app)
        job_id = generation_jobs.create_job()
        collected: dict[str, list] = {}

        asyncio.run(
            generate_plan(project, ["tree"], "heuristic", on_items=lambda t, items: collected.__setitem__(t, items))
        )
        generation_jobs.add_partial(job_id, "tree", collected["tree"])

        status = client.get(f"/api/projects/{project.id}/generate/{job_id}").json()
        body = client.get(f"/api/projects/{project.id}/generate/{job_id}/partial").json()

        assert status["partial_counts"] == {"tree": len(collected["tree"])} and status["partial_version"] == 1
        assert body["version"] == 1 and body["counts"] == {"tree": len(collected["tree"])}
        assert len(body["features"]["features"]) == len(collected["tree"])
        assert {f["properties"]["planting_type"] for f in body["features"]["features"]} == {"tree"}
