import math

from vscore.score import codebase_total, confidence, v_points


def test_zero_terms_add_nothing_instead_of_zeroing_the_score():
    assert v_points(0, 0, 2) == 0
    assert v_points(0, 3, 2) == 2 * math.log2(4)
    assert v_points(255, 0, 2) == 8


def test_missing_parts_make_v_null_never_zero():
    assert v_points(None, 3, 2) is None
    assert v_points(10, None, 2) is None


def test_confidence_levels():
    assert confidence(1.0, 2) == "full"
    assert confidence(0.0, 0) == "full"
    assert confidence(None, 2) == "partial"
    assert confidence(1.0, 2, partial_history=True) == "partial"
    assert confidence(None, None) == "none"


def test_codebase_total_counts_each_measured_file_once():
    total = codebase_total([3.0, None, 4.0], [10, 10, 10])
    assert total["W"] == 7.0
    assert total["V"] == math.log2(8)
