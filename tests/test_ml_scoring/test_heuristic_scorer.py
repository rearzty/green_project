from shapely.geometry import Point, Polygon

from geo_engine.model import PlantingCandidate
from geo_engine.norms import load_norms
from ml_scoring.heuristic_scorer import HeuristicScorer

NORMS = load_norms()


def test_score_is_in_unit_range_and_has_rationale():
    scorer = HeuristicScorer(NORMS)
    candidate = PlantingCandidate(geometry=Point(0, 0), planting_type="tree", clearance_m=3.0, zoning="recreational")
    score, rationale = scorer.score(candidate)
    assert 0.0 <= score <= 1.0
    assert rationale


def test_more_clearance_scores_at_least_as_high_all_else_equal():
    scorer = HeuristicScorer(NORMS)
    close = PlantingCandidate(geometry=Point(0, 0), planting_type="tree", clearance_m=0.1, zoning="recreational")
    far = PlantingCandidate(geometry=Point(0, 0), planting_type="tree", clearance_m=10.0, zoning="recreational")
    score_close, _ = scorer.score(close)
    score_far, _ = scorer.score(far)
    assert score_far >= score_close


def test_lawn_candidate_uses_area_not_canopy_default():
    scorer = HeuristicScorer(NORMS)
    small = PlantingCandidate(
        geometry=Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]), planting_type="lawn", clearance_m=5.0, area_m2=1.0
    )
    large = PlantingCandidate(
        geometry=Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]), planting_type="lawn", clearance_m=5.0, area_m2=100.0
    )
    score_small, _ = scorer.score(small)
    score_large, _ = scorer.score(large)
    assert score_large >= score_small
