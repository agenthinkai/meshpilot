import pytest

from routers.metrics import HOURS_PER_MONTH, cloud_comparison

MILLION = 1_000_000


def test_cheap_cloud_model_is_never_reached_at_this_speed():
    # 2M tokens in 20,000s is 100 tokens/s, or 262.8M a month at most.
    result = cloud_comparison("cheap", 0.10, 0.40, MILLION, MILLION, 20_000, 100)
    assert result["same_tokens_usd"] == pytest.approx(0.5)
    assert result["break_even_tokens_per_month"] == 400 * MILLION
    assert result["busy_pct"] == pytest.approx(400 / (100 * 3600 * HOURS_PER_MONTH / MILLION) * 100, abs=0.1)
    assert result["busy_pct"] > 100


def test_dearer_cloud_model_is_reached_part_way_through_the_month():
    result = cloud_comparison("dear", 1, 5, MILLION, MILLION, 20_000, 100)
    assert result["break_even_tokens_per_month"] == pytest.approx(100 / 3 * MILLION, abs=1)
    assert result["busy_pct"] == pytest.approx(12.7, abs=0.1)


def test_output_heavy_use_reaches_break_even_sooner():
    output_heavy = cloud_comparison("m", 1, 5, 0, MILLION, 10_000, 100)
    input_heavy = cloud_comparison("m", 1, 5, MILLION, 0, 10_000, 100)
    assert output_heavy["break_even_tokens_per_month"] < input_heavy["break_even_tokens_per_month"]


def test_nothing_is_claimed_before_there_is_usage():
    result = cloud_comparison("m", 1, 5, 0, 0, 0, 100)
    assert result["same_tokens_usd"] == 0
    assert result["break_even_tokens_per_month"] is None and result["busy_pct"] is None
