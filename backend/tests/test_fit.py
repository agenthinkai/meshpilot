import json
from pathlib import Path

import pytest

from core.fit import (
    build_report, cloud_monthly_usd, estimate_economics, normalize, score_case, summarize_model,
)
from routers.fit import FitSuite

PRICING = {"cloud_label": "Cloud", "cloud_usd_per_m_input": 0.10, "cloud_usd_per_m_output": 0.40,
           "server_label": "Server", "server_usd_per_hour": 0.2}
WORKLOAD = {"requests_per_day": 5000, "avg_input_tokens": 400, "avg_output_tokens": 150}


def case(task="qa", score=1.0, passed=True, latency=1.0, **extra):
    return {"task": task, "score": score, "passed": passed, "latency_s": latency, "missed": [],
            "prompt_tokens": 100, "completion_tokens": 50, "prompt_s": 1.0, "generation_s": 5.0, **extra}


def test_sample_suite_is_valid_and_scorable():
    raw = json.loads((Path(__file__).parent.parent / "core" / "fit_sample.json").read_text(encoding="utf-8"))
    suite = FitSuite.model_validate(raw)
    assert len(suite.cases) == 15


def test_normalize_makes_equivalent_answers_match():
    assert normalize("2,317") == "2317"
    assert normalize("3-hour 18-minute") == "3 hour 18 minute"
    assert normalize("2026-03-14") == "2026 03 14"


# Answers a naive substring check scored wrongly in the first manual test.
def test_equivalent_wordings_score_correctly():
    groups = [["3 hours 18", "3 hour 18"]]
    assert score_case("a 3-hour 18-minute outage", {"contains_all": groups})["passed"]
    assert not score_case("a 3-hour outage", {"contains_all": groups})["passed"]

    expect = {"json_fields": {"date": ["14 march 2026", "2026 03 14"]}}
    assert score_case('{"date": "2026-03-14"}', expect)["passed"]


def test_exact_ignores_case_and_punctuation():
    assert score_case(" Billing. ", {"exact": ["billing"]})["passed"]
    assert not score_case("billing or fraud", {"exact": ["billing"]})["passed"]


def test_json_fields_accepts_code_fences_and_scores_partially():
    expect = {"json_fields": {"a": ["x"], "b": ["y"]}}
    assert score_case('```json\n{"a": "x", "b": "y"}\n```', expect)["passed"]
    partial = score_case('{"a": "x", "b": "z"}', expect)
    assert partial["score"] == pytest.approx(0.667) and not partial["passed"]
    assert score_case("no json at all", expect)["score"] == 0


def test_declines_and_sentence_limit():
    assert score_case("That is not stated.", {"declines": True})["passed"]
    assert not score_case("Customers receive $100.", {"declines": True})["passed"]
    assert score_case("One. Two. Three.", {"max_sentences": 3})["passed"]
    assert not score_case("One. Two. Three. Four.", {"max_sentences": 3})["passed"]


def test_case_without_checks_is_rejected():
    with pytest.raises(ValueError):
        score_case("anything", {})


def test_summary_excludes_failed_requests_from_speed():
    failed = case(score=0, passed=False, latency=120.0, error="timeout")
    model = summarize_model("m", "M", [case(), failed], 500.0)
    assert model["overall_accuracy"] == 0.5
    assert model["errors"] == 1
    assert model["latency_p95_s"] == 1.0
    assert model["generation_tps"] == 10.0


def test_economics_arithmetic():
    model = {"prompt_tps": 100.0, "generation_tps": 10.0}
    result = estimate_economics(model, WORKLOAD, PRICING)
    assert result["seconds_per_request"] == 19.0
    assert result["requests_per_server_per_day"] == 2728
    assert result["servers_needed"] == 2
    assert result["monthly_server_usd"] == 292.0
    assert cloud_monthly_usd(WORKLOAD, PRICING) == 15.0


def _models():
    fast = summarize_model("fast", "Fast", [case(task="a", latency=1.0), case(task="b", score=0.2, passed=False)], 500.0)
    slow = summarize_model("slow", "Slow", [case(task="a", latency=9.0), case(task="b", score=0.5, passed=False)], 5000.0)
    return [fast, slow]


def test_recommendation_prefers_fastest_model_meeting_threshold():
    report = build_report({"name": "x", "cases": [{}] * 4}, _models(), {"threads_per_model": 4}, PRICING)
    assert report["recommendations"]["a"]["model_id"] == "fast"
    assert report["recommendations"]["a"]["meets_threshold"] is True


def test_report_says_when_no_model_is_good_enough():
    report = build_report({"name": "x", "cases": [{}] * 4}, _models(), {"threads_per_model": 4}, PRICING)
    assert report["recommendations"]["b"]["model_id"] == "slow"
    assert report["recommendations"]["b"]["meets_threshold"] is False


def test_small_samples_are_flagged():
    small = build_report({"name": "x", "cases": [{}] * 4}, _models(), {}, PRICING)
    assert "directional" in small["caveats"][0]
    large = build_report({"name": "x", "cases": [{}] * 30}, _models(), {}, PRICING)
    assert not any("directional" in c for c in large["caveats"])
