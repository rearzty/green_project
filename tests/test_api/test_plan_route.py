"""GET /api/projects/{id}/plans/{plan_id} отдаёт рецепт плана целиком: панель и
Юна после перезагрузки страницы или переключения версии восстанавливают
настройки именно этого плана (frontend/lib/planRecipes.ts)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from shapely.geometry import Point

import backend.app.db.session as store
from backend.app.db.models import Plan, PlantingItemRow, Project
from backend.app.main import app


def test_get_plan_returns_its_whole_recipe():
    project = Project(name="Улица")
    plan = Plan(
        project_id=project.id,
        scoring_mode="ml",
        planting_types=["tree", "shrub"],
        tree_spacing_m=7.0,
        shrub_spacing_m=0.7,
        tree_rows=2,
        trees_in_paving=True,
        item_count=1,
    )
    plan.items = [PlantingItemRow(plan_id=plan.id, geometry=Point(0, 0), planting_type="tree", species="Липа мелколистная")]
    plan.project = project
    project.plans = [plan]
    store.save_project(project)
    try:
        body = TestClient(app).get(f"/api/projects/{project.id}/plans/{plan.id}").json()
    finally:
        store._projects.pop(project.id, None)

    assert (body["scoring_mode"], body["planting_types"], body["tree_rows"], body["trees_in_paving"]) == (
        "ml", ["tree", "shrub"], 2, True
    )
    assert (body["tree_spacing_m"], body["shrub_spacing_m"]) == (7.0, 0.7)
    assert len(body["features"]["features"]) == 1
