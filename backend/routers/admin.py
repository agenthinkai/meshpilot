"""MeshPilot Admin Router — user management, system stats (admin-only)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core import audit
from core.database import InferenceLog, ModelRecord, User, UserRole, UserTier, get_db
from core.security import generate_temporary_password, hash_password, require_admin

router = APIRouter()


class UserAdminOut(BaseModel):
    id:         str
    email:      str
    username:   str
    role:       str
    tier:       str
    is_active:  bool
    must_change_password: bool
    created_at: datetime
    last_login: Optional[datetime]


class CreateUserRequest(BaseModel):
    email: EmailStr
    name:  str = Field(min_length=2, max_length=64)
    role:  UserRole = UserRole.USER


class TemporaryPasswordOut(BaseModel):
    user: UserAdminOut
    temporary_password: str   # shown ONCE


def _user_out(u: User) -> UserAdminOut:
    return UserAdminOut(
        id=u.id, email=u.email, username=u.username,
        role=u.role.value, tier=u.tier.value,
        is_active=u.is_active, must_change_password=u.must_change_password,
        created_at=u.created_at, last_login=u.last_login,
    )


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
    result = await db.execute(select(User).order_by(User.created_at))
    return [_user_out(u) for u in result.scalars().all()]


@router.post("/users", response_model=TemporaryPasswordOut, status_code=201)
async def create_user(
    body:  CreateUserRequest,
    db:    AsyncSession = Depends(get_db),
    admin: User         = Depends(require_admin),
):
    name, email = body.name.strip(), body.email.lower()
    if len(name) < 2:
        raise HTTPException(400, "Enter a name")
    existing = await db.execute(select(User).where(or_(func.lower(User.email) == email, User.username == name)))
    clash = existing.scalars().first()
    if clash:
        field = "email" if clash.email.lower() == email else "name"
        raise HTTPException(409, f"Someone with this {field} already has an account")

    temporary_password = generate_temporary_password()
    # Team tier: rate limited per person, without the free tier's lower API key limit.
    user = User(
        email=email, username=name, hashed_password=hash_password(temporary_password),
        role=body.role, tier=UserTier.TEAM, is_active=True, must_change_password=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.flush()
    await audit.record_event(
        "user.created", actor_user_id=admin.id, actor_email=admin.email, status="created",
        detail={"target_user": user.id, "target_email": email, "role": body.role.value},
    )
    return TemporaryPasswordOut(user=_user_out(user), temporary_password=temporary_password)


@router.post("/users/{user_id}/reset-password", response_model=TemporaryPasswordOut)
async def reset_user_password(
    user_id: str,
    db:      AsyncSession = Depends(get_db),
    admin:   User         = Depends(require_admin),
):
    if user_id == admin.id:
        raise HTTPException(400, "Use Change password for your own account")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")

    temporary_password = generate_temporary_password()
    user.hashed_password = hash_password(temporary_password)
    user.must_change_password = True
    user.password_changed_at = datetime.now(timezone.utc)
    await audit.record_event(
        "user.password_reset", actor_user_id=admin.id, actor_email=admin.email, status="updated",
        detail={"target_user": user.id, "target_email": user.email},
    )
    return TemporaryPasswordOut(user=_user_out(user), temporary_password=temporary_password)


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

    # Only ever blocks the caller, so at least one active admin always remains.
    if user.id == admin.id and (body.is_active is False or body.role not in (None, UserRole.ADMIN.value)):
        raise HTTPException(400, "You can't remove your own admin access. Ask another admin to do it.")

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

    await audit.record_event(
        "user.updated", actor_user_id=admin.id, actor_email=admin.email, status="updated",
        detail={"target_user": user.id, **body.model_dump(exclude_none=True)},
    )

    return _user_out(user)


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
