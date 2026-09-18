"""backend/app/db/session.py: the in-process project store's weight-bounded
LRU eviction. Plain dict/threading code with no I/O -- unlike
layer_raster.py/exclusion_cache.py's DB-adjacent neighbours (see
test_layer_raster.py's own docstring on this repo's convention of not
exercising DB code through pytest), there's nothing here that needs a live
server to exercise honestly.
"""

from __future__ import annotations

import pytest

import backend.app.db.session as store
from backend.app.db.models import Layer, Plan, PlantingItemRow, Project


def _project_with_layers(count: int, name: str) -> Project:
    project = Project(name=name)
    project.layers = [Layer(project_id=project.id, kind="zone", object_type="test", geometry=None) for _ in range(count)]
    return project


@pytest.fixture(autouse=True)
def _isolated_store():
    """session.py has no reset hook (it's meant to live for the process's
    whole lifetime) -- reach into its module global directly rather than
    adding test-only API to production code, and make sure one test's
    projects never leak into the next."""
    store._projects.clear()
    yield
    store._projects.clear()


class TestEviction:
    def test_projects_under_budget_are_never_evicted(self):
        store.save_project(_project_with_layers(10, "a"))
        store.save_project(_project_with_layers(10, "b"))

        assert len(store._projects) == 2

    def test_least_recently_touched_project_is_evicted_first(self, monkeypatch):
        monkeypatch.setattr(store, "_MAX_TOTAL_WEIGHT", 25)
        a = _project_with_layers(10, "a")
        b = _project_with_layers(10, "b")
        store.save_project(a)
        store.save_project(b)
        store.get_project(a.id)  # touches `a` again, so `b` is now the LRU one

        c = _project_with_layers(10, "c")
        store.save_project(c)  # total would be 30 > 25 -- one project must go

        assert store.get_project(b.id) is None
        assert store.get_project(a.id) is not None
        assert store.get_project(c.id) is not None

    def test_never_evicts_the_last_remaining_project(self, monkeypatch):
        """A single project heavier than the whole budget on its own must
        still survive -- evicting the project someone just uploaded would be
        worse than going over budget until the next save."""
        monkeypatch.setattr(store, "_MAX_TOTAL_WEIGHT", 5)
        huge = _project_with_layers(1000, "huge")

        store.save_project(huge)

        assert store.get_project(huge.id) is not None

    def test_materialized_plan_items_count_toward_weight_not_just_layers(self, monkeypatch):
        monkeypatch.setattr(store, "_MAX_TOTAL_WEIGHT", 25)
        light = _project_with_layers(5, "light")
        heavy = _project_with_layers(5, "heavy")
        heavy.plans = [
            Plan(
                project_id=heavy.id,
                items=[PlantingItemRow(plan_id="p", geometry=None, planting_type="tree") for _ in range(30)],
            )
        ]

        store.save_project(light)
        store.save_project(heavy)  # 5 (light) + 5 + 30 (heavy) = 40 > 25

        assert store.get_project(light.id) is None
        assert store.get_project(heavy.id) is not None

    def test_get_project_refreshes_recency_even_without_a_new_save(self, monkeypatch):
        """Eviction only runs inside save_project (see session.py's own
        docstring on why), but get_project still has to update LRU order --
        otherwise a project someone is actively reading from, but never
        re-saving, would look just as stale as one nobody has touched
        since upload."""
        monkeypatch.setattr(store, "_MAX_TOTAL_WEIGHT", 25)
        a = _project_with_layers(10, "a")
        b = _project_with_layers(10, "b")
        store.save_project(a)
        store.save_project(b)

        store.get_project(b.id)  # `b` is now the most recently used, not `a`

        c = _project_with_layers(10, "c")
        store.save_project(c)

        assert store.get_project(a.id) is None
        assert store.get_project(b.id) is not None
        assert store.get_project(c.id) is not None
