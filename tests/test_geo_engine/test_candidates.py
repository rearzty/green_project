"""Tests for candidates.py's safety valve against pathologically dense
requests -- see TooManyCandidatesError's own docstring for the live incident
that motivated it (a 300x300m territory at shrub's minimum spacing, 0.3m,
was still running past 10 minutes and 5GB RAM when this didn't exist yet).
The check happens before any of the expensive sampling work, so these tests
stay fast even though they're exercising "would have been catastrophic" cases.
"""

import time

import pytest
from shapely.geometry import Point, Polygon, box

from geo_engine.candidates import TooManyCandidatesError, ZoningIndex, generate_point_candidates
from geo_engine.model import Zone
from geo_engine.norms import load_norms

NORMS = load_norms()


def test_minimum_shrub_spacing_on_a_modest_territory_is_rejected():
    # Reproduces this session's live incident at a smaller, deterministic
    # scale -- shrub_spacing_m at its own allowed floor (GenerateRequest's
    # ge=0.3, schemas/plan.py) on a 300x300m buildable area.
    territory = box(0, 0, 300, 300)
    norms = NORMS.with_spacing_override("shrub", 0.3)
    with pytest.raises(TooManyCandidatesError):
        generate_point_candidates(territory, None, "shrub", norms)


def test_minimum_tree_spacing_on_a_modest_territory_is_rejected():
    # Tree's own floor needs a bit more area than shrub's to cross the cap
    # (0.5m > 0.3m, so fewer raw samples per m² at the same territory size) --
    # 500x500m is still well inside "modest", nowhere near real-scale (1.5km).
    territory = box(0, 0, 500, 500)
    norms = NORMS.with_spacing_override("tree", 0.5)
    with pytest.raises(TooManyCandidatesError):
        generate_point_candidates(territory, None, "tree", norms)


def test_default_spacing_on_a_real_scale_territory_is_not_rejected():
    # ~1.5x1.5km, this project's own documented real-world scale (CLAUDE.md),
    # at planting_norms.yaml's own defaults -- must stay well clear of the
    # cap, or the safety valve would be blocking the app's main use case.
    territory = box(0, 0, 1500, 1500)
    for planting_type in ("tree", "shrub"):
        candidates = generate_point_candidates(territory, None, planting_type, NORMS)
        assert len(candidates) > 0


def test_default_spacing_on_a_small_territory_is_not_rejected():
    territory = box(0, 0, 100, 80)
    for planting_type in ("tree", "shrub"):
        candidates = generate_point_candidates(territory, None, planting_type, NORMS)
        assert len(candidates) > 0


class TestZoningIndex:
    """Regression for a second live find on the same street as the
    existing_greenery one (ml_scoring/features.py): `_zoning_at()` looped
    `for zone in zoning_zones: if zone.zone_type == "zoning" ...` per
    candidate over the project's *entire*, unfiltered zones list. Real
    Mosgeotrest DXF data never produces zone_type="zoning" at all (no DXF
    layer map maps anything to it), so this scanned hundreds of thousands of
    irrelevant zones and found nothing, every single time. Confirmed live:
    331,140 zones x ~8,000 raw tree candidates on "1. Олимпийская деревня"
    -- 282 seconds in generate_candidates alone, for one planting type.
    """

    def test_finds_the_zoning_category_a_point_actually_falls_in(self):
        residential = Zone(
            geometry=Polygon([(0, 0), (0, 10), (10, 10), (10, 0)]),
            zone_type="zoning",
            attrs={"zoning_category": "residential"},
        )
        industrial = Zone(
            geometry=Polygon([(20, 0), (20, 10), (30, 10), (30, 0)]),
            zone_type="zoning",
            attrs={"zoning_category": "industrial"},
        )
        index = ZoningIndex([residential, industrial])

        assert index.category_at(Point(5, 5)) == "residential"
        assert index.category_at(Point(25, 5)) == "industrial"
        assert index.category_at(Point(50, 50)) is None

    def test_non_zoning_zones_are_ignored_not_mistaken_for_zoning(self):
        building = Zone(geometry=Polygon([(0, 0), (0, 10), (10, 10), (10, 0)]), zone_type="building", attrs={})
        index = ZoningIndex([building])

        assert index.category_at(Point(5, 5)) is None

    def test_empty_or_missing_zoning_zones_is_a_plain_none_not_an_error(self):
        assert ZoningIndex(None).category_at(Point(0, 0)) is None
        assert ZoningIndex([]).category_at(Point(0, 0)) is None

    def test_scanning_hundreds_of_thousands_of_non_zoning_zones_is_fast(self):
        """Not a synthetic worst case -- 331,140 is the real zone count from
        one pilot street, essentially none of it zone_type="zoning"."""
        zones = [
            Zone(geometry=Point(i % 500, i // 500).buffer(1.0), zone_type="existing_greenery", attrs={})
            for i in range(300_000)
        ]
        index = ZoningIndex(zones)
        points = [Point(i * 0.3, i * 0.2) for i in range(8_000)]

        started = time.perf_counter()
        for point in points:
            index.category_at(point)
        elapsed = time.perf_counter() - started

        # The naive per-candidate scan measured ~282s for a comparable count
        # on real data. 5s leaves generous headroom for a slower CI machine
        # without letting a real regression back to O(zones) slip through.
        assert elapsed < 5.0, f"scanning {len(points)} candidates took {elapsed:.1f}s -- looks like the O(n) path again"
