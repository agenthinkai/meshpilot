"""Fit report: scores local models on a customer's own tasks, then estimates
capacity and cost from what was measured. Scoring and maths only; the worker in
workers/fit_worker.py runs the models.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from typing import Dict, List, Optional

ACCURACY_THRESHOLD = 0.9
TARGET_UTILISATION = 0.6   # headroom: don't plan a server to run flat out
HOURS_PER_MONTH = 730
DAYS_PER_MONTH = 30
SMALL_SAMPLE_CASES = 30

DECLINE_PHRASES = [
    "not stated", "not mentioned", "not specified", "not provided", "not included",
    "does not contain", "does not state", "does not mention", "does not specify", "no information",
]


def normalize(text: str) -> str:
    """Lets equivalent answers match: 2,317 and 2317, 3-hour and 3 hour."""
    text = re.sub(r"(?<=\d),(?=\d)", "", str(text).lower())
    text = re.sub(r"[-–—_/]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _has_any(text: str, options: List[str]) -> bool:
    return any(normalize(option) in text for option in options)


def _parse_json(output: str) -> Optional[dict]:
    match = re.search(r"\{.*\}", output, re.S)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _count_sentences(output: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?])\s+", output.strip()) if s])


def score_case(output: str, expect: dict) -> dict:
    """Returns {score: 0..1, passed, missed: [what was wrong]}. All checks in
    `expect` count equally. See core/fit_sample.json for the format."""
    checks: List[tuple] = []
    text = normalize(output)

    if "exact" in expect:
        got = re.sub(r"^\W+|\W+$", "", text)
        wanted = [normalize(v) for v in expect["exact"]]
        checks.append((got in wanted, f"expected one of {expect['exact']}"))

    for group in expect.get("contains_all", []):
        checks.append((_has_any(text, group), f"missing {group[0]!r}"))

    if "json_fields" in expect:
        parsed = _parse_json(output)
        checks.append((parsed is not None, "not valid JSON"))
        for key, options in expect["json_fields"].items():
            value = normalize((parsed or {}).get(key, ""))
            checks.append((bool(value) and _has_any(value, options), f"field {key!r} wrong or missing"))

    if expect.get("declines"):
        checks.append((_has_any(text, DECLINE_PHRASES), "answered instead of saying the information isn't there"))

    if "max_sentences" in expect:
        count = _count_sentences(output)
        checks.append((count <= expect["max_sentences"], f"{count} sentences, limit {expect['max_sentences']}"))

    if not checks:
        raise ValueError("Case has no checks in `expect`")
    hits = sum(1 for ok, _ in checks if ok)
    return {
        "score": round(hits / len(checks), 3),
        "passed": hits == len(checks),
        "missed": [why for ok, why in checks if not ok],
    }


def _percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, math.ceil(pct * len(ordered)) - 1)], 2)


def summarize_model(model_id: str, name: str, cases: List[dict], memory_mb: Optional[float]) -> dict:
    tasks: Dict[str, dict] = {}
    for task in sorted({c["task"] for c in cases}):
        rows = [c for c in cases if c["task"] == task]
        latencies = [c["latency_s"] for c in rows if not c.get("error")]
        tasks[task] = {
            "cases": len(rows),
            "passed": sum(1 for c in rows if c["passed"]),
            "accuracy": round(statistics.mean(c["score"] for c in rows), 3),
            "mean_latency_s": round(statistics.mean(latencies), 2) if latencies else None,
        }

    ok = [c for c in cases if not c.get("error")]
    latencies = [c["latency_s"] for c in ok]
    total_time = sum(latencies)
    prompt_seconds = sum(c.get("prompt_s") or 0 for c in ok)
    gen_seconds = sum(c.get("generation_s") or 0 for c in ok)
    prompt_tokens = sum(c.get("prompt_tokens") or 0 for c in ok)
    completion_tokens = sum(c.get("completion_tokens") or 0 for c in ok)
    return {
        "model_id": model_id,
        "name": name,
        "memory_mb": memory_mb,
        "tasks": tasks,
        "overall_accuracy": round(statistics.mean(c["score"] for c in cases), 3) if cases else None,
        "errors": len(cases) - len(ok),
        "latency_p50_s": _percentile(latencies, 0.5),
        "latency_p95_s": _percentile(latencies, 0.95),
        "total_time_s": round(total_time, 1),
        "prompt_tps": round(prompt_tokens / prompt_seconds, 1) if prompt_seconds else None,
        "generation_tps": round(completion_tokens / gen_seconds, 1) if gen_seconds else None,
        "cases": cases,
    }


def estimate_economics(model: dict, workload: dict, pricing: dict) -> Optional[dict]:
    """Servers and monthly cost for the customer's volume. Assumes one request
    at a time per server, which is conservative, and is not a load test."""
    prompt_tps, gen_tps = model.get("prompt_tps"), model.get("generation_tps")
    if not prompt_tps or not gen_tps:
        return None
    seconds_per_request = workload["avg_input_tokens"] / prompt_tps + workload["avg_output_tokens"] / gen_tps
    per_server_per_day = 86400 / seconds_per_request * TARGET_UTILISATION
    servers = max(1, math.ceil(workload["requests_per_day"] / per_server_per_day))
    return {
        "seconds_per_request": round(seconds_per_request, 2),
        "requests_per_server_per_day": int(per_server_per_day),
        "servers_needed": servers,
        "monthly_server_usd": round(servers * pricing["server_usd_per_hour"] * HOURS_PER_MONTH, 2),
    }


def cloud_monthly_usd(workload: dict, pricing: dict) -> float:
    requests = workload["requests_per_day"] * DAYS_PER_MONTH
    return round(
        requests * workload["avg_input_tokens"] / 1e6 * pricing["cloud_usd_per_m_input"]
        + requests * workload["avg_output_tokens"] / 1e6 * pricing["cloud_usd_per_m_output"],
        2,
    )


def _pick_for_task(models: List[dict], task: str) -> Optional[dict]:
    candidates = [m for m in models if task in m["tasks"]]
    if not candidates:
        return None
    good = [m for m in candidates if m["tasks"][task]["accuracy"] >= ACCURACY_THRESHOLD]
    if good:
        chosen = min(good, key=lambda m: m["tasks"][task]["mean_latency_s"] or math.inf)
    else:
        chosen = max(candidates, key=lambda m: m["tasks"][task]["accuracy"])
    return {
        "model_id": chosen["model_id"],
        "name": chosen["name"],
        "accuracy": chosen["tasks"][task]["accuracy"],
        "mean_latency_s": chosen["tasks"][task]["mean_latency_s"],
        "meets_threshold": bool(good),
    }


def build_report(suite: dict, models: List[dict], hardware: dict, pricing: dict) -> dict:
    workload = suite.get("workload")
    for model in models:
        model["economics"] = estimate_economics(model, workload, pricing) if workload else None

    tasks = sorted({task for m in models for task in m["tasks"]})
    case_count = len(suite["cases"])
    caveats = [
        "Scoring is automated pattern matching. Review the raw outputs before relying on a result.",
        "Capacity assumes one request at a time per server and was not load tested.",
        "Costs are estimates from the listed prices and measured speed. They exclude staff, power and support.",
        f"Speeds were measured on this host, with each model limited to {hardware.get('threads_per_model', 'its')} threads.",
    ]
    too_long = sum(1 for m in models for c in m["cases"] if "document too long for this model" in c["missed"])
    if too_long:
        caveats.insert(0, f"{too_long} results failed because the document was too long to fit in one request. "
                          "Those documents were not read, so split them or use a larger context.")
    if case_count < SMALL_SAMPLE_CASES:
        caveats.insert(0, f"Only {case_count} test cases: treat accuracy as directional, not as a benchmark.")

    return {
        "suite_name": suite.get("name") or "Untitled",
        "case_count": case_count,
        "accuracy_threshold": ACCURACY_THRESHOLD,
        "hardware": hardware,
        "pricing": pricing,
        "workload": workload,
        "cloud_monthly_usd": cloud_monthly_usd(workload, pricing) if workload else None,
        "recommendations": {task: _pick_for_task(models, task) for task in tasks},
        "models": models,
        "caveats": caveats,
    }
