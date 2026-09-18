"""backend/app/services/exclusion_cache.py: per-key locking around the
setback-zone/territory caches. The correctness property that matters here
isn't the caching itself (already exercised indirectly through
test_edit_service.py's validate/move/retype paths) but the specific bug this
session fixed: a single lock shared across every project used to serialize
`with_exclusion_zone`/`with_territory` calls that touched completely
unrelated geometry. These tests use empty-layer projects (build_exclusion_zone
degrades to an empty GEOMETRYCOLLECTION with nothing to buffer, see its own
"no constraints at all" branch) so they're fast and don't depend on the
pilot dataset -- the property under test is locking behaviour, not geometry.
"""

from __future__ import annotations

import threading

from backend.app.db.models import Project
from backend.app.services import exclusion_cache


class TestLockFor:
    def test_same_key_returns_the_same_lock_object(self):
        registry: dict = {}
        first = exclusion_cache._lock_for(registry, "key")
        second = exclusion_cache._lock_for(registry, "key")
        assert first is second

    def test_different_keys_return_different_lock_objects(self):
        registry: dict = {}
        a = exclusion_cache._lock_for(registry, "key-a")
        b = exclusion_cache._lock_for(registry, "key-b")
        assert a is not b


class TestExclusionZoneCaching:
    def test_second_call_with_the_same_key_reuses_the_built_zone(self):
        project = Project(name="cache-test")

        first = exclusion_cache.with_exclusion_zone(project, "tree", lambda zone: zone)
        second = exclusion_cache.with_exclusion_zone(project, "tree", lambda zone: zone)

        assert first is second  # same object -> rebuilt once, not twice

    def test_different_planting_types_get_different_zones(self):
        project = Project(name="cache-test")

        tree_zone = exclusion_cache.with_exclusion_zone(project, "tree", lambda zone: zone)
        shrub_zone = exclusion_cache.with_exclusion_zone(project, "shrub", lambda zone: zone)

        assert tree_zone is not shrub_zone


class TestPerKeyLocking:
    def test_two_different_projects_do_not_serialize_on_each_other(self):
        """The actual regression this session fixed: with the old single
        global lock, project B's call would block until project A's `fn`
        returned, even though they never touch the same geometry. If this
        test hangs (times out waiting on `a_started` or `thread.join`), the
        two keys are back to sharing one lock."""
        project_a = Project(name="a")
        project_b = Project(name="b")

        a_started = threading.Event()
        release_a = threading.Event()

        def slow_fn(zone):
            a_started.set()
            assert release_a.wait(timeout=2), "test itself never released project A"
            return "a-done"

        thread = threading.Thread(target=lambda: exclusion_cache.with_exclusion_zone(project_a, "tree", slow_fn))
        thread.start()
        try:
            assert a_started.wait(timeout=2), "project A's fn never started"

            # While A is still blocked inside its fn, B must complete promptly.
            result_b = exclusion_cache.with_exclusion_zone(project_b, "shrub", lambda zone: "b-done")
            assert result_b == "b-done"
        finally:
            release_a.set()
            thread.join(timeout=2)
        assert not thread.is_alive()

    def test_same_project_and_type_still_serializes(self):
        """Not a regression to remove entirely -- a GEOS-prepared geometry's
        lazily-built index isn't guaranteed safe to query concurrently, so
        two callers asking about the *same* zone must still wait on each
        other."""
        project = Project(name="same-key")

        first_started = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()

        def first_fn(zone):
            first_started.set()
            release_first.wait(timeout=2)
            return "first-done"

        thread = threading.Thread(target=lambda: exclusion_cache.with_exclusion_zone(project, "tree", first_fn))
        thread.start()
        try:
            assert first_started.wait(timeout=2)

            def second_fn(zone):
                second_started.set()
                return "second-done"

            second_thread = threading.Thread(
                target=lambda: exclusion_cache.with_exclusion_zone(project, "tree", second_fn)
            )
            second_thread.start()
            try:
                # The second call must NOT be able to start while the first
                # still holds the lock for this same key.
                assert not second_started.wait(timeout=0.3)
            finally:
                release_first.set()
                second_thread.join(timeout=2)
                assert not second_thread.is_alive()
        finally:
            thread.join(timeout=2)
        assert not thread.is_alive()
