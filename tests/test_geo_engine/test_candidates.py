"""Tests for candidates.py's safety valve against pathologically dense
requests -- see TooManyCandidatesError's own docstring for the live incident
that motivated it (a 300x300m territory at shrub's minimum spacing, 0.3m,
was still running past 10 minutes and 5GB RAM when this didn't exist yet).
The check happens before any of the expensive sampling work, so these tests
stay fast even though they're exercising "would have been catastrophic" cases.
"""

import pytest
from shapely.geometry import box

from geo_engine.candidates import TooManyCandidatesError, generate_point_candidates
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
