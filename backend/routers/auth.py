"""MeshPilot Auth Router — login, API key management."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core import audit
from core.database import APIKey, User, UserTier, get_db
from core.ratelimit import check_login_allowed, clear_login_failures, record_login_failure
from core.security import (
    create_access_token,
    generate_api_key,
    get_user_with_any_password,
    hash_password,
    password_problem,
    verify_password,
    get_current_user_jwt,
)

router = APIRouter()

# Verified against when the email is unknown, so response time doesn't reveal
# which accounts exist.
_DUMMY_PASSWORD_HASH = hash_password("meshpilot-no-such-user")


def _client_ip(request: Request) -> str:
    # nginx sets this, and the API isn't reachable except through nginx.
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")


# ── Schemas ───────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email:    EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type:   str = "bearer"
    user_id:      str
    username:     str
    role:         str
    tier:         str
    must_change_password: bool = False


class UserResponse(BaseModel):
    id:         str
    email:      str
    username:   str
    role:       str
    tier:       str
    created_at: datetime
    must_change_password: bool


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password:     str


class APIKeyResponse(BaseModel):
    id:         str
    name:       str
    key_prefix: str
    is_active:  bool
    created_at: datetime
    last_used:  Optional[datetime]
    requests_count: int


class CreateAPIKeyRequest(BaseModel):
    name: str = "Default Key"


class CreateAPIKeyResponse(BaseModel):
    id:      str
    name:    str
    api_key: str   # shown ONCE
    prefix:  str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    ip = _client_ip(request)
    await check_login_allowed(body.email, ip)

    result = await db.execute(select(User).where(func.lower(User.email) == body.email.lower()))
    user = result.scalars().first()
    password_ok = verify_password(body.password, user.hashed_password if user else _DUMMY_PASSWORD_HASH)
    if not user or not password_ok:
        await record_login_failure(body.email, ip)
        await audit.record_event("auth.login_failed", actor_email=body.email, status="failed", detail={"ip": ip})
        raise HTTPException(401, "Invalid credentials")
    if not user.is_active:
        raise HTTPException(403, "Account disabled")

    await clear_login_failures(body.email, ip)
    await audit.record_event("auth.login", actor_user_id=user.id, actor_email=user.email,
                             status="success", detail={"ip": ip})
    user.last_login = datetime.now(timezone.utc)
    token = create_access_token({"sub": user.id, "role": user.role.value})
    return TokenResponse(
        access_token=token,
        user_id=user.id,
        username=user.username,
        role=user.role.value,
        tier=user.tier.value,
        must_change_password=user.must_change_password,
    )


@router.get("/me", response_model=UserResponse)
async def me(user: User = Depends(get_user_with_any_password)):
    return UserResponse(
        id=user.id, email=user.email, username=user.username,
        role=user.role.value, tier=user.tier.value, created_at=user.created_at,
        must_change_password=user.must_change_password,
    )


@router.post("/change-password", response_model=TokenResponse)
async def change_password(
    body:    ChangePasswordRequest,
    request: Request,
    user:    User = Depends(get_user_with_any_password),
):
    ip = _client_ip(request)
    await check_login_allowed(user.email, ip)
    if not verify_password(body.current_password, user.hashed_password):
        await record_login_failure(user.email, ip)
        await audit.record_event("auth.password_change_failed", actor_user_id=user.id, actor_email=user.email,
                                 status="failed", detail={"ip": ip})
        raise HTTPException(400, "Your current password is incorrect")
    problem = password_problem(body.new_password)
    if problem:
        raise HTTPException(400, problem)
    if body.new_password == body.current_password:
        raise HTTPException(400, "Choose a password you aren't using now")

    await clear_login_failures(user.email, ip)
    user.hashed_password = hash_password(body.new_password)
    user.must_change_password = False
    user.password_changed_at = datetime.now(timezone.utc)
    await audit.record_event("auth.password_changed", actor_user_id=user.id, actor_email=user.email,
                             status="success", detail={"ip": ip})
    # Other sessions end now, so this one needs a fresh token to carry on.
    return TokenResponse(
        access_token=create_access_token({"sub": user.id, "role": user.role.value}),
        user_id=user.id, username=user.username, role=user.role.value, tier=user.tier.value,
    )


@router.post("/api-keys", response_model=CreateAPIKeyResponse, status_code=201)
async def create_api_key(
    body: CreateAPIKeyRequest,
    user: User = Depends(get_current_user_jwt),
    db:   AsyncSession = Depends(get_db),
):
    # Tier limits
    result = await db.execute(select(APIKey).where(APIKey.user_id == user.id, APIKey.is_active == True))
    active_keys = result.scalars().all()
    limits = {UserTier.FREE: 2, UserTier.PRO: 10, UserTier.TEAM: 50, UserTier.ENTERPRISE: 200}
    if len(active_keys) >= limits.get(user.tier, 2):
        raise HTTPException(429, f"API key limit reached for {user.tier.value} tier")

    full_key, key_hash, key_prefix = generate_api_key()
    api_key = APIKey(user_id=user.id, key_hash=key_hash, key_prefix=key_prefix, name=body.name)
    db.add(api_key)
    await db.flush()
    await audit.record_event("api_key.created", actor_user_id=user.id, actor_email=user.email,
                             actor_key_prefix=key_prefix, status="created", detail={"name": body.name})

    return CreateAPIKeyResponse(id=api_key.id, name=api_key.name, api_key=full_key, prefix=key_prefix)


@router.get("/api-keys", response_model=List[APIKeyResponse])
async def list_api_keys(user: User = Depends(get_current_user_jwt), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(APIKey).where(APIKey.user_id == user.id))
    keys = result.scalars().all()
    return [APIKeyResponse(
        id=k.id, name=k.name, key_prefix=k.key_prefix,
        is_active=k.is_active, created_at=k.created_at,
        last_used=k.last_used, requests_count=k.requests_count or 0,
    ) for k in keys]


@router.delete("/api-keys/{key_id}", status_code=204)
async def revoke_api_key(
    key_id: str,
    user:   User = Depends(get_current_user_jwt),
    db:     AsyncSession = Depends(get_db),
):
    result = await db.execute(select(APIKey).where(APIKey.id == key_id, APIKey.user_id == user.id))
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(404, "API key not found")
    key.is_active = False
    await audit.record_event("api_key.revoked", actor_user_id=user.id, actor_email=user.email,
                             actor_key_prefix=key.key_prefix, status="revoked", detail={"name": key.name})
