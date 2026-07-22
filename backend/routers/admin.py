"""MeshPilot Admin Router — user management, system stats (admin-only)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import InferenceLog, ModelRecord, User, UserRole, UserTier, get_db
from core.security import require_admin

router = APIRouter()


class UserAdminOut(BaseModel):
    id:         str
    email:      str
    username:   str
    role:       str
    tier:       str
    is_active:  bool
    created_at: datetime
    last_login: Optional[datetime]


class SystemStats(BaseModel):
    total_users:      int
    total_models:     int
    total_inferences: int
    avg_latency_ms:   Optional[float]
    avg_throughput_tps: Optional[float]


class UpdateUserRequest(BaseModel):
    role: Optional[str] = None
    tier: Optional[str] = None
    is_active: Optional[bool] = None


@router.get("/users", response_model=List[UserAdminOut])
async def list_users(
    db:    AsyncSession = Depends(get_db),
    admin: User         = Depends(require_admin),
):
    result = await db.execute(select(User))
    return [UserAdminOut(
        id=u.id, email=u.email, username=u.username,
        role=u.role.value, tier=u.tier.value,
        is_active=u.is_active, created_at=u.created_at, last_login=u.last_login,
    ) for u in result.scalars().all()]


@router.patch("/users/{user_id}", response_model=UserAdminOut)
async def update_user(
    user_id: str,
    body:    UpdateUserRequest,
    db:      AsyncSession = Depends(get_db),
    admin:   User         = Depends(require_admin),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")

    if body.role is not None:
        try:
            user.role = UserRole(body.role)
        except ValueError:
            raise HTTPException(400, f"Invalid role: {body.role}")
    if body.tier is not None:
        try:
            user.tier = UserTier(body.tier)
        except ValueError:
            raise HTTPException(400, f"Invalid tier: {body.tier}")
    if body.is_active is not None:
        user.is_active = body.is_active

    return UserAdminOut(
        id=user.id, email=user.email, username=user.username,
        role=user.role.value, tier=user.tier.value,
        is_active=user.is_active, created_at=user.created_at, last_login=user.last_login,
    )


@router.get("/stats", response_model=SystemStats)
async def system_stats(
    db:    AsyncSession = Depends(get_db),
    admin: User         = Depends(require_admin),
):
    users_count  = (await db.execute(select(func.count(User.id)))).scalar_one()
    models_count = (await db.execute(select(func.count(ModelRecord.id)))).scalar_one()
    infer_count  = (await db.execute(select(func.count(InferenceLog.id)))).scalar_one()
    avg_latency  = (await db.execute(select(func.avg(InferenceLog.latency_ms)))).scalar_one()
    avg_tps      = (await db.execute(select(func.avg(InferenceLog.throughput_tps)))).scalar_one()

    return SystemStats(
        total_users=users_count,
        total_models=models_count,
        total_inferences=infer_count,
        avg_latency_ms=round(avg_latency, 1) if avg_latency else None,
        avg_throughput_tps=round(avg_tps, 2) if avg_tps else None,
    )
