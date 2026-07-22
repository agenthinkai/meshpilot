"""MeshPilot Auth Router — signup, login, API key management."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import APIKey, User, UserRole, UserTier, get_db
from core.security import (
    create_access_token,
    generate_api_key,
    hash_password,
    verify_password,
    get_current_user_jwt,
)

router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────────────

class SignupRequest(BaseModel):
    email:    EmailStr
    username: str
    password: str

    @field_validator("username")
    @classmethod
    def username_valid(cls, v):
        if len(v) < 3 or len(v) > 32:
            raise ValueError("Username must be 3–32 characters")
        if not v.replace("_", "").replace("-", "").isalnum():
            raise ValueError("Username may only contain letters, digits, hyphens, underscores")
        return v.lower()

    @field_validator("password")
    @classmethod
    def password_strength(cls, v):
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


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


class UserResponse(BaseModel):
    id:         str
    email:      str
    username:   str
    role:       str
    tier:       str
    created_at: datetime


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

@router.post("/signup", response_model=TokenResponse, status_code=201)
async def signup(body: SignupRequest, db: AsyncSession = Depends(get_db)):
    # Check uniqueness
    result = await db.execute(select(User).where(User.email == body.email))
    if result.scalar_one_or_none():
        raise HTTPException(400, "Email already registered")
    result = await db.execute(select(User).where(User.username == body.username))
    if result.scalar_one_or_none():
        raise HTTPException(400, "Username already taken")

    # First user becomes admin
    count_result = await db.execute(select(User))
    is_first = count_result.scalars().first() is None

    user = User(
        email           = body.email,
        username        = body.username,
        hashed_password = hash_password(body.password),
        role            = UserRole.ADMIN if is_first else UserRole.USER,
        tier            = UserTier.FREE,
    )
    db.add(user)
    await db.flush()

    token = create_access_token({"sub": user.id, "role": user.role.value})
    return TokenResponse(
        access_token=token,
        user_id=user.id,
        username=user.username,
        role=user.role.value,
        tier=user.tier.value,
    )


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(401, "Invalid credentials")
    if not user.is_active:
        raise HTTPException(403, "Account disabled")

    user.last_login = datetime.now(timezone.utc)
    token = create_access_token({"sub": user.id, "role": user.role.value})
    return TokenResponse(
        access_token=token,
        user_id=user.id,
        username=user.username,
        role=user.role.value,
        tier=user.tier.value,
    )


@router.get("/me", response_model=UserResponse)
async def me(user: User = Depends(get_current_user_jwt)):
    return UserResponse(
        id=user.id, email=user.email, username=user.username,
        role=user.role.value, tier=user.tier.value, created_at=user.created_at,
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
