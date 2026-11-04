"""MeshPilot Metrics Router — usage stats for the dashboard."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database import CompareRun, InferenceLog, InferenceStatus, ModelRecord, User, get_db
from core.security import StatsScope, get_current_user_jwt, stats_filter
from core.cpu_detect import get_cpu_profile

router = APIRouter()

HOURS_PER_MONTH = 730
CLOUD_PRICES_CHECKED = "2026-11-04"
# Standard-tier list prices in USD per 1M tokens (label, input, output), from the
# OpenAI, Google and Anthropic pricing pages. The price set in .env comes first.
CLOUD_PRICE_CHOICES = [
    ("Gemini 2.5 Flash-Lite", 0.10, 0.40),
    ("Claude Haiku 5.5", 0.10, 0.50),
    ("Gemini 3.1 Flash-Lite", 0.25, 1.50),
    ("GPT-5 mini", 0.25, 2.00),
    ("GPT-5.4 mini", 0.75, 4.50),
    ("Claude Sonnet 5.5", 2.00, 10.00),
]


def cloud_comparison(label: str, usd_per_m_input: float, usd_per_m_output: float,
                     prompt_tokens: int, completion_tokens: int,
                     local_seconds: float, server_usd_per_month: float) -> dict:
    """What the tokens so far would cost on a cloud model, and the monthly volume
    at which a server paid for around the clock costs the same as that model."""
    tokens = prompt_tokens + completion_tokens
    cost = (prompt_tokens * usd_per_m_input + completion_tokens * usd_per_m_output) / 1_000_000
    break_even = busy_pct = None
    if tokens:
        break_even = server_usd_per_month / (cost / tokens)
        if local_seconds:
            capacity = tokens / local_seconds * 3600 * HOURS_PER_MONTH
            busy_pct = break_even / capacity * 100
    return {
        "label": label,
        "usd_per_m_input": usd_per_m_input,
        "usd_per_m_output": usd_per_m_output,
        "same_tokens_usd": round(cost, 6),
        "break_even_tokens_per_month": round(break_even) if break_even else None,
        "busy_pct": round(busy_pct, 1) if busy_pct else None,
    }


class CloudOption(BaseModel):
    label:                       str
    usd_per_m_input:             float
    usd_per_m_output:            float
    same_tokens_usd:             float
    break_even_tokens_per_month: Optional[int]
    # Share of the month the server must be generating to reach break-even.
    # Over 100 means it can't reach it at the speed measured so far.
    busy_pct:                    Optional[float]


class DashboardStats(BaseModel):
    total_requests:     int
    requests_24h:       int
    avg_latency_ms:     Optional[float]
    avg_throughput_tps: Optional[float]
    total_tokens:       int
    models_count:       int
    active_people_30d:  Optional[int] = None  # company scope only
    cpu_profile:        dict


class SavingsStats(BaseModel):
    # Measured from compare runs (None until at least one run exists)
    compare_runs:             int
    mesh_tps:                 Optional[float]
    baseline_tps:             Optional[float]
    speedup_x:                Optional[float]
    compute_seconds_saved:    Optional[float]
    mesh_memory_mb:           Optional[float]
    baseline_memory_mb:       Optional[float]
    memory_reduction_pct:     Optional[float]
    # Measured across all completed inference
    tokens_on_prem:           int
    # Estimates: measured speed and tokens x the list prices below
    mesh_usd_per_m_tokens:    Optional[float]
    baseline_usd_per_m_tokens:Optional[float]
    compute_cost_reduction_pct:Optional[float]
    local_compute_usd:        float
    cloud_usd:                float
    cloud_usd_per_m_input:    float
    cloud_usd_per_m_output:   float
    cloud_price_label:        str
    server_usd_per_hour:      float
    server_price_label:       str
    server_usd_per_month:     float
    capacity_tokens_per_month:Optional[int]
    input_share_pct:          Optional[float]
    cloud_prices_checked:     str
    cloud_options:            List[CloudOption]


class LatencyBucket(BaseModel):
    hour:       str
    avg_ms:     float
    count:      int


@router.get("/dashboard", response_model=DashboardStats)
async def dashboard_stats(
    scope: StatsScope   = "mine",
    db:    AsyncSession = Depends(get_db),
    user:  User         = Depends(get_current_user_jwt),
):
    now = datetime.now(timezone.utc)
    scoped = stats_filter(InferenceLog.user_id, scope, user)

    total_req = (await db.execute(
        select(func.count(InferenceLog.id)).where(*scoped)
    )).scalar_one()

    req_24h = (await db.execute(
        select(func.count(InferenceLog.id)).where(
            *scoped,
            InferenceLog.created_at >= now - timedelta(hours=24),
        )
    )).scalar_one()

    avg_lat = (await db.execute(
        select(func.avg(InferenceLog.latency_ms)).where(
            *scoped,
            InferenceLog.status == InferenceStatus.COMPLETED,
        )
    )).scalar_one()

    avg_tps = (await db.execute(
        select(func.avg(InferenceLog.throughput_tps)).where(
            *scoped,
            InferenceLog.status == InferenceStatus.COMPLETED,
        )
    )).scalar_one()

    total_tokens = (await db.execute(
        select(func.sum(InferenceLog.completion_tokens)).where(*scoped)
    )).scalar_one() or 0

    visible_models = [] if scope == "company" else [
        (ModelRecord.owner_id == user.id) | (ModelRecord.is_public == True)
    ]
    models_count = (await db.execute(
        select(func.count(ModelRecord.id)).where(*visible_models)
    )).scalar_one()

    active_people = None
    if scope == "company":
        active_people = (await db.execute(
            select(func.count(func.distinct(InferenceLog.user_id))).where(
                InferenceLog.created_at >= now - timedelta(days=30),
            )
        )).scalar_one()

    return DashboardStats(
        total_requests=total_req,
        requests_24h=req_24h,
        avg_latency_ms=round(avg_lat, 1) if avg_lat else None,
        avg_throughput_tps=round(avg_tps, 2) if avg_tps else None,
        total_tokens=int(total_tokens),
        models_count=models_count,
        active_people_30d=active_people,
        cpu_profile=get_cpu_profile().to_dict(),
    )


@router.get("/savings", response_model=SavingsStats)
async def savings_stats(
    scope: StatsScope   = "mine",
    db:    AsyncSession = Depends(get_db),
    user:  User         = Depends(get_current_user_jwt),
):
    runs = (await db.execute(
        select(
            func.count(CompareRun.id),
            func.coalesce(func.sum(CompareRun.mesh_prompt_tokens), 0),
            func.coalesce(func.sum(CompareRun.mesh_completion_tokens), 0),
            func.coalesce(func.sum(CompareRun.mesh_latency_ms), 0.0),
            func.coalesce(func.sum(CompareRun.baseline_completion_tokens), 0),
            func.coalesce(func.sum(CompareRun.baseline_latency_ms), 0.0),
            func.avg(CompareRun.mesh_memory_mb),
            func.avg(CompareRun.baseline_memory_mb),
        ).where(*stats_filter(CompareRun.user_id, scope, user))
    )).one()
    run_count, mesh_prompt, mesh_tokens, mesh_ms, base_tokens, base_ms, mesh_mem, base_mem = runs

    logs = (await db.execute(
        select(
            func.coalesce(func.sum(InferenceLog.prompt_tokens), 0),
            func.coalesce(func.sum(InferenceLog.completion_tokens), 0),
            func.coalesce(func.sum(InferenceLog.latency_ms), 0.0),
        ).where(
            *stats_filter(InferenceLog.user_id, scope, user),
            InferenceLog.status == InferenceStatus.COMPLETED,
        )
    )).one()

    prompt_tokens     = int(logs[0]) + int(mesh_prompt)
    completion_tokens = int(logs[1]) + int(mesh_tokens)
    local_seconds     = (float(logs[2]) + float(mesh_ms)) / 1000

    # Pooled tokens / pooled time, so long runs count for more than short ones.
    mesh_tps = mesh_tokens / (mesh_ms / 1000) if mesh_ms else None
    base_tps = base_tokens / (base_ms / 1000) if base_ms else None
    both = mesh_tps and base_tps

    rate = settings.SERVER_USD_PER_HOUR
    per_m = lambda tps: rate / (tps * 3600) * 1_000_000
    mesh_cost = per_m(mesh_tps) if both else None
    base_cost = per_m(base_tps) if both else None
    both_mem = mesh_mem and base_mem

    tokens = prompt_tokens + completion_tokens
    server_month = rate * HOURS_PER_MONTH
    configured = (settings.CLOUD_PRICE_LABEL, settings.CLOUD_INPUT_USD_PER_M, settings.CLOUD_OUTPUT_USD_PER_M)
    prices = [configured] + [p for p in CLOUD_PRICE_CHOICES if p[0] != configured[0]]

    return SavingsStats(
        compare_runs=run_count,
        mesh_tps=round(mesh_tps, 2) if both else None,
        baseline_tps=round(base_tps, 2) if both else None,
        speedup_x=round(mesh_tps / base_tps, 2) if both else None,
        compute_seconds_saved=round(mesh_tokens / base_tps - mesh_ms / 1000, 1) if both else None,
        mesh_memory_mb=round(mesh_mem, 1) if both_mem else None,
        baseline_memory_mb=round(base_mem, 1) if both_mem else None,
        memory_reduction_pct=round((1 - mesh_mem / base_mem) * 100, 1) if both_mem else None,
        tokens_on_prem=prompt_tokens + completion_tokens,
        mesh_usd_per_m_tokens=round(mesh_cost, 4) if both else None,
        baseline_usd_per_m_tokens=round(base_cost, 4) if both else None,
        compute_cost_reduction_pct=round((1 - mesh_cost / base_cost) * 100, 1) if both else None,
        local_compute_usd=round(local_seconds / 3600 * rate, 6),
        cloud_usd=round(
            prompt_tokens / 1_000_000 * settings.CLOUD_INPUT_USD_PER_M
            + completion_tokens / 1_000_000 * settings.CLOUD_OUTPUT_USD_PER_M, 6
        ),
        cloud_usd_per_m_input=settings.CLOUD_INPUT_USD_PER_M,
        cloud_usd_per_m_output=settings.CLOUD_OUTPUT_USD_PER_M,
        cloud_price_label=settings.CLOUD_PRICE_LABEL,
        server_usd_per_hour=rate,
        server_price_label=settings.SERVER_PRICE_LABEL,
        server_usd_per_month=round(server_month, 2),
        capacity_tokens_per_month=round(tokens / local_seconds * 3600 * HOURS_PER_MONTH) if tokens and local_seconds else None,
        input_share_pct=round(prompt_tokens / tokens * 100, 1) if tokens else None,
        cloud_prices_checked=CLOUD_PRICES_CHECKED,
        cloud_options=[
            cloud_comparison(*price, prompt_tokens, completion_tokens, local_seconds, server_month)
            for price in prices
        ],
    )
