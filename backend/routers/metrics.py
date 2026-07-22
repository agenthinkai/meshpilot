"""MeshPilot Metrics Router — usage stats for the dashboard."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import InferenceLog, InferenceStatus, ModelRecord, User, get_db
from core.security import get_current_user_jwt
from core.cpu_detect import get_cpu_profile

router = APIRouter()


class DashboardStats(BaseModel):
    total_requests:     int
    requests_24h:       int
    avg_latency_ms:     Optional[float]
    avg_throughput_tps: Optional[float]
    total_tokens:       int
    models_count:       int
    cpu_profile:        dict


class LatencyBucket(BaseModel):
    hour:       str
    avg_ms:     float
    count:      int


@router.get("/dashboard", response_model=DashboardStats)
async def dashboard_stats(
    db:   AsyncSession = Depends(get_db),
    user: User         = Depends(get_current_user_jwt),
):
    since_24h = datetime.now(timezone.utc) - timedelta(hours=24)

    total_req = (await db.execute(
        select(func.count(InferenceLog.id)).where(InferenceLog.user_id == user.id)
    )).scalar_one()

    req_24h = (await db.execute(
        select(func.count(InferenceLog.id)).where(
            InferenceLog.user_id == user.id,
            InferenceLog.created_at >= since_24h,
        )
    )).scalar_one()

    avg_lat = (await db.execute(
        select(func.avg(InferenceLog.latency_ms)).where(
            InferenceLog.user_id == user.id,
            InferenceLog.status == InferenceStatus.COMPLETED,
        )
    )).scalar_one()

    avg_tps = (await db.execute(
        select(func.avg(InferenceLog.throughput_tps)).where(
            InferenceLog.user_id == user.id,
            InferenceLog.status == InferenceStatus.COMPLETED,
        )
    )).scalar_one()

    total_tokens = (await db.execute(
        select(func.sum(InferenceLog.completion_tokens)).where(
            InferenceLog.user_id == user.id,
        )
    )).scalar_one() or 0

    models_count = (await db.execute(
        select(func.count(ModelRecord.id)).where(
            (ModelRecord.owner_id == user.id) | (ModelRecord.is_public == True)
        )
    )).scalar_one()

    return DashboardStats(
        total_requests=total_req,
        requests_24h=req_24h,
        avg_latency_ms=round(avg_lat, 1) if avg_lat else None,
        avg_throughput_tps=round(avg_tps, 2) if avg_tps else None,
        total_tokens=int(total_tokens),
        models_count=models_count,
        cpu_profile=get_cpu_profile().to_dict(),
    )
