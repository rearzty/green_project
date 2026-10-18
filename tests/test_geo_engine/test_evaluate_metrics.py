"""Метрики сравнения плана с деревьями проектировщика (scripts/evaluate.py)."""

from __future__ import annotations

import numpy as np
import pytest

from scripts.evaluate import compare, summarize


def test_recall_and_precision_count_matches_within_the_radius():
    ref = np.array([[0.0, 0.0], [10.0, 0.0], [20.0, 0.0], [30.0, 0.0]])
    ours = np.array([[0.5, 0.0], [10.0, 2.0], [50.0, 0.0]])
    result = compare(ours, ref, radius=3.0)
    assert result["recall"] == pytest.approx(0.5)  # нашли 2 из 4 его мест
    assert result["precision"] == pytest.approx(2 / 3)  # 2 из 3 наших — на его местах
    assert result["f1"] == pytest.approx(2 * 0.5 * (2 / 3) / (0.5 + 2 / 3))


def test_empty_sides_give_zero_not_an_error():
    assert compare(np.zeros((0, 2)), np.array([[0.0, 0.0]]), 3.0)["f1"] == 0.0
    assert compare(np.array([[0.0, 0.0]]), np.zeros((0, 2)), 3.0)["f1"] == 0.0


def test_summary_skips_streets_without_a_usable_reference():
    rows = [
        {
            "id": "a", "trees": 10, "recall": 0.4, "precision": 0.2, "f1": 0.27, "ref_legal_share": 0.5,
            "recall_all_ref": 0.3, "precision_all_ref": 0.25, "f1_all_ref": 0.27, "compliance": 1.0,
        },
        {"id": "b", "ref_legal_share": None, "compliance": 1.0},
        {"id": "c", "error": "boom"},
    ]
    summary = summarize(rows)
    assert summary["streets_with_reference"] == 1
    assert summary["mean_recall"] == 0.4
    assert summary["mean_f1_all_ref"] == 0.27
    assert summary["trees_total"] == 10
    assert summary["min_compliance"] == 1.0
