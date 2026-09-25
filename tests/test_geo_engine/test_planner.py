"""plan_items' multiprocessing path (geo_engine/planner.py).

Real-data verification: 156.06s -> 120.74s on "1. Олимпийская деревня",
measured against the pre-patterns _plan_type_items (plain scatter +
greedy_select), before row/group placement (geo_engine/patterns.py) was
merged in on top of it. The parallelization carried over onto the
patterns-integrated version unchanged (same extraction, same threshold, same
futures-in-submission-order guarantee) -- re-verified on the same street with
patterns in place: 1512.15s -> 379.72s combined-run wall time, byte-identical
output (13124 items, same species/counts, all compliant) once
geo_engine/candidates.py::ExclusionIndex fixed a *second*, larger bottleneck
that patterns.py's group/curtain pattern introduced (see CLAUDE.md's
`planner.plan_items()`/куртины paragraphs) -- that second fix is unrelated to
parallelism itself, just found while re-measuring it.

What *is* cheap to check here, and worth checking regardless of what's inside
_plan_type_items (a multiprocessing path can silently diverge from its
sequential twin -- pickling truncates/reorders something, a future completes
and gets appended out of order, a new picklability requirement sneaks in via
patterns.py's RowGuide/PlantingCandidate, ...): that forcing the parallel
path on a small scene gives the exact same items as the sequential path, and
that the threshold gate actually gates -- a real ProcessPoolExecutor is not
spun up below it.
"""

from __future__ import annotations

from unittest.mock import patch

from shapely.geometry import Point

import geo_engine.planner as planner
from geo_engine.model import PlantingItem
from geo_engine.norms import load_norms
from geo_engine.planner import DEFAULT_DENSITY_PER_HA, _limit_by_density, plan_items
from scripts.generate_synthetic_data import generate_synthetic_territory

NORMS = load_norms()


def _constant_score_fn(candidates):
    """Module-level, not a closure -- ProcessPoolExecutor has to pickle
    whatever plan_items is handed as score_fn, and a function nested inside a
    test method can't be pickled at all."""
    return [(1.0, "test") for _ in candidates]


def _generate(synthetic_scene, threshold):
    territory, utilities, zones = synthetic_scene["territory"], synthetic_scene["utilities"], synthetic_scene["zones"]
    with patch.object(planner, "_PARALLEL_PLAN_THRESHOLD", threshold):
        return plan_items("planner-test", utilities, zones, territory, ["tree", "shrub"], _constant_score_fn, NORMS)


class TestParallelMatchesSequential:
    def test_forced_parallel_path_yields_identical_items(self, synthetic_scene):
        """threshold=0 forces the ProcessPoolExecutor path even on the tiny
        synthetic scene (a handful of utilities/zones, nowhere near the
        real-data default threshold) -- the point is comparing its output to
        the sequential path's, not exercising it at real scale."""
        sequential = _generate(synthetic_scene, threshold=10**9)
        parallel = _generate(synthetic_scene, threshold=0)

        assert len(sequential) == len(parallel) > 0
        # Order matters too: futures are collected in submission order (the
        # order of `planting_types`), not completion order -- if that ever
        # regressed, this would catch a silent type-interleaving change.
        seq_summary = [(i.planting_type, i.species, i.geometry.wkt) for i in sequential]
        par_summary = [(i.planting_type, i.species, i.geometry.wkt) for i in parallel]
        assert seq_summary == par_summary

    def test_forced_parallel_path_keeps_planting_types_grouped_in_submission_order(self, synthetic_scene):
        parallel = _generate(synthetic_scene, threshold=0)
        types_seen = [item.planting_type for item in parallel]
        # tree items all before shrub items -- exactly what a single
        # ProcessPoolExecutor task per type, collected in submission order,
        # guarantees; interleaving would mean something is chunking within a
        # type across processes, which plan_items never does.
        assert types_seen == sorted(types_seen, key=["tree", "shrub"].index)


class TestParallelThresholdGate:
    def test_small_scene_does_not_spin_up_a_process_pool(self, synthetic_scene):
        """Default threshold, tiny synthetic scene -- ProcessPoolExecutor
        must not be constructed at all, only the sequential per-type loop."""
        territory, utilities, zones = synthetic_scene["territory"], synthetic_scene["utilities"], synthetic_scene["zones"]
        with patch.object(planner, "ProcessPoolExecutor") as mock_pool:
            items = plan_items("planner-test", utilities, zones, territory, ["tree", "shrub"], _constant_score_fn, NORMS)
        mock_pool.assert_not_called()
        assert items

    def test_forced_threshold_does_spin_up_a_process_pool(self, synthetic_scene):
        territory, utilities, zones = synthetic_scene["territory"], synthetic_scene["utilities"], synthetic_scene["zones"]
        with patch.object(planner, "_PARALLEL_PLAN_THRESHOLD", 0), patch.object(
            planner, "ProcessPoolExecutor", wraps=planner.ProcessPoolExecutor
        ) as mock_pool:
            items = plan_items("planner-test", utilities, zones, territory, ["tree", "shrub"], _constant_score_fn, NORMS)
        mock_pool.assert_called_once()
        assert items

    def test_a_single_planting_type_never_parallelizes(self, synthetic_scene):
        """Nothing to parallelize across with only one type -- the pool
        should stay unused even with the threshold forced to 0."""
        territory, utilities, zones = synthetic_scene["territory"], synthetic_scene["utilities"], synthetic_scene["zones"]
        with patch.object(planner, "_PARALLEL_PLAN_THRESHOLD", 0), patch.object(planner, "ProcessPoolExecutor") as mock_pool:
            items = plan_items("planner-test", utilities, zones, territory, ["tree"], _constant_score_fn, NORMS)
        mock_pool.assert_not_called()
        assert items


class TestDefaultDensity:
    """DEFAULT_DENSITY_PER_HA -- without any cap, greedy_select fills every
    legally available spot ("сколько влезает", not "сколько нужно"),
    confirmed live as both unrealistic (235 curtains, 12577 shrubs on one
    real street, every 14m regardless of whether a designer would put an
    accent group there) and the dominant cost on real scale (see CLAUDE.md's
    "Куртины кустарника" paragraph). plan_items() now applies a practice-based
    per-type default instead of requiring every caller to opt in, reusing the
    exact tree=25/shrub=250 figures already validated by eye on the reference
    street (docs/decision_log.md) rather than inventing new numbers.

    Uses a bigger-than-fixture synthetic territory (not the 100x80m
    `synthetic_scene`) so the uncapped tree count genuinely exceeds the
    default cap -- on the small fixture the cap might never actually engage,
    which would make these assertions pass without testing anything.
    """

    def _big_scene(self):
        return generate_synthetic_territory(seed=2, width_m=300.0, height_m=300.0, n_utilities=2, n_buildings=1)

    def test_omitting_density_per_ha_caps_output_to_the_default(self):
        scene = self._big_scene()
        territory, utilities, zones = scene["territory"], scene["utilities"], scene["zones"]
        area_ha = territory.area / 10_000
        allowed = max(1, round(DEFAULT_DENSITY_PER_HA["tree"] * area_ha))

        # pattern="scatter": row is now exempt from this budget entirely --
        # its count comes from real guide length at the required pitch, not
        # from "how much looks right per hectare" (see DEFAULT_DENSITY_PER_HA's
        # module comment and _limit_row_and_group_by_density's docstring) --
        # so with the default "auto" pattern, a row long enough could legally
        # push the total past `allowed`. Forcing scatter-only isolates the
        # discretionary path this test actually means to check.
        uncapped = plan_items(
            "density-test", utilities, zones, territory, ["tree"], _constant_score_fn, NORMS,
            pattern="scatter", density_per_ha={"tree": 0},
        )
        defaulted = plan_items(
            "density-test", utilities, zones, territory, ["tree"], _constant_score_fn, NORMS, pattern="scatter"
        )

        assert len(uncapped) > allowed, "fixture too small for this test to be meaningful -- the cap never engages"
        assert len(defaulted) <= allowed

    def test_explicit_override_replaces_only_that_type(self):
        scene = self._big_scene()
        territory, utilities, zones = scene["territory"], scene["utilities"], scene["zones"]
        area_ha = territory.area / 10_000
        tree_allowed = max(1, round(DEFAULT_DENSITY_PER_HA["tree"] * area_ha))
        # A deliberately tiny override (1/ha) -- far below DEFAULT_DENSITY_PER_HA["shrub"]
        # (250/ha), so if the override were silently ignored this would still
        # pass at the default's much higher allowance.
        shrub_override_allowed = max(1, round(1 * area_ha))

        # pattern="scatter": see the comment in the test above -- row is exempt
        # from density_per_ha now, and this test is about the override
        # mechanism, not the row/scatter split.
        items = plan_items(
            "density-test", utilities, zones, territory, ["tree", "shrub"], _constant_score_fn, NORMS,
            pattern="scatter", density_per_ha={"shrub": 1},
        )
        shrub_count = len([i for i in items if i.planting_type == "shrub"])
        tree_count = len([i for i in items if i.planting_type == "tree"])

        assert shrub_count <= shrub_override_allowed
        # tree wasn't touched by the override -- still governed by its own default, not shrub's
        assert tree_count <= tree_allowed

    def test_zero_means_unlimited_not_zero_items(self):
        """The risk this guards: `_limit_by_density` treating 0 as `< allowed`
        instead of `no cap at all` would silently generate nothing for a type
        someone explicitly disabled the cap for -- the opposite of what
        `--density tree=0` promises in the CLI help text."""
        scene = self._big_scene()
        territory, utilities, zones = scene["territory"], scene["utilities"], scene["zones"]

        zero = plan_items("density-test", utilities, zones, territory, ["tree"], _constant_score_fn, NORMS, density_per_ha={"tree": 0})
        effectively_unlimited = plan_items(
            "density-test", utilities, zones, territory, ["tree"], _constant_score_fn, NORMS,
            density_per_ha={"tree": 10**6},
        )

        assert len(zero) > 0
        assert len(zero) == len(effectively_unlimited)


def _item(score: float, x: float = 0.0) -> PlantingItem:
    return PlantingItem(geometry=Point(x, 0), planting_type="tree", species="test", score=score, rationale="")


class TestLimitByDensity:
    """The pure counting/selection logic DEFAULT_DENSITY_PER_HA relies on --
    no geometry pipeline needed, just the arithmetic and the "keep the
    best-scoring ones" selection."""

    def test_none_or_non_positive_density_is_a_no_op(self):
        items = [_item(1.0), _item(2.0)]
        assert _limit_by_density(items, area_m2=10_000, density_per_ha=None) == items
        assert _limit_by_density(items, area_m2=10_000, density_per_ha=0) == items
        assert _limit_by_density(items, area_m2=10_000, density_per_ha=-5) == items

    def test_zero_area_is_a_no_op_rather_than_dividing_by_zero(self):
        items = [_item(1.0)]
        assert _limit_by_density(items, area_m2=0, density_per_ha=100) == items

    def test_fewer_items_than_the_allowance_are_left_untouched(self):
        items = [_item(1.0), _item(2.0)]
        # 1 ha at 100/ha allows 100 -- far more than the 2 items given
        assert _limit_by_density(items, area_m2=10_000, density_per_ha=100) == items

    def test_keeps_the_best_scoring_items_up_to_the_allowance(self):
        items = [_item(3.0), _item(1.0), _item(5.0), _item(2.0), _item(4.0)]
        # 1 ha at 20/ha -> round(20 * 1) = 20, capped by len(items); use a
        # smaller area so the allowance (2) actually bites.
        limited = _limit_by_density(items, area_m2=1_000, density_per_ha=20)
        assert [i.score for i in limited] == [5.0, 4.0]

    def test_allowance_rounds_and_never_goes_below_one(self):
        items = [_item(1.0), _item(2.0), _item(3.0)]
        # 100 m2 at 1/ha -> 0.01 rounds to 0, but at least 1 item survives
        limited = _limit_by_density(items, area_m2=100, density_per_ha=1)
        assert len(limited) == 1
        assert limited[0].score == 3.0
