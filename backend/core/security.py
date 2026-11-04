"""
MeshPilot Security — JWT tokens, API key management, RBAC.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, APIKeyHeader
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database import APIKey, User, UserRole, get_db

# ── Password hashing ──────────────────────────────────────────────────────────

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_BYTES = 72  # bcrypt only reads this many
TEMPORARY_PASSWORD_LENGTH = 16
_TEMPORARY_PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # nothing easily misread


def password_problem(password: str) -> Optional[str]:
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters"
    if len(password.encode()) > MAX_PASSWORD_BYTES:
        return f"Use at most {MAX_PASSWORD_BYTES} bytes"
    return None


def generate_temporary_password() -> str:
    return "".join(secrets.choice(_TEMPORARY_PASSWORD_ALPHABET) for _ in range(TEMPORARY_PASSWORD_LENGTH))


# ── JWT ───────────────────────────────────────────────────────────────────────

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire, "iat": datetime.now(timezone.utc)})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ── API Key generation & hashing ──────────────────────────────────────────────

def generate_api_key() -> tuple[str, str, str]:
    """
    Returns (full_key, key_hash, key_prefix).
    full_key is shown once to the user; only key_hash is stored.
    """
    alphabet = string.ascii_letters + string.digits
    raw = "".join(secrets.choice(alphabet) for _ in range(40))
    full_key = f"{settings.API_KEY_PREFIX}{raw}"
    key_hash = hashlib.sha256(full_key.encode()).hexdigest()
    key_prefix = full_key[:12]  # "mp_" + 9 chars for display
    return full_key, key_hash, key_prefix


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


# ── Webhook HMAC signing ──────────────────────────────────────────────────────

def sign_webhook_payload(payload: bytes, secret: str) -> str:
    """Return HMAC-SHA256 hex digest for webhook payload verification."""
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


# ── FastAPI dependency helpers ────────────────────────────────────────────────

bearer_scheme = HTTPBearer(auto_error=False)
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _token_outlived_password(user: User, payload: dict) -> bool:
    """A password change or reset ends every session started before it."""
    changed = user.password_changed_at
    return bool(changed) and payload.get("iat", 0) < int(changed.timestamp())


async def get_user_with_any_password(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Also accepts someone still on a temporary password, so only use it for
    the endpoints they need in order to replace it."""
    if not credentials:
        raise HTTPException(status_code=401, detail="Bearer token required")
    payload = decode_token(credentials.credentials)
    user_id: str = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")
    result = await db.execute(select(User).where(User.id == user_id, User.is_active == True))
    user = result.scalar_one_or_none()
    if not user or _token_outlived_password(user, payload):
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user


async def get_current_user_jwt(user: User = Depends(get_user_with_any_password)) -> User:
    if user.must_change_password:
        raise HTTPException(status_code=403, detail="Set a new password first")
    return user


async def get_current_user_api_key(
    api_key: Optional[str] = Security(api_key_header),
    db: AsyncSession = Depends(get_db),
) -> User:
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required (X-API-Key header)")
    key_hash = hash_api_key(api_key)
    result = await db.execute(
        select(APIKey).where(APIKey.key_hash == key_hash, APIKey.is_active == True)
    )
    api_key_record = result.scalar_one_or_none()
    if not api_key_record:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")

    # Update last_used
    from datetime import datetime, timezone
    api_key_record.last_used = datetime.now(timezone.utc)
    api_key_record.requests_count = (api_key_record.requests_count or 0) + 1

    result = await db.execute(
        select(User).where(User.id == api_key_record.user_id, User.is_active == True)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user


async def _optional_jwt_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> Optional[User]:
    if not credentials:
        return None
    try:
        payload = decode_token(credentials.credentials)
    except HTTPException:
        return None
    user_id: str = payload.get("sub")
    if not user_id:
        return None
    result = await db.execute(select(User).where(User.id == user_id, User.is_active == True))
    user = result.scalar_one_or_none()
    if not user or user.must_change_password or _token_outlived_password(user, payload):
        return None
    return user


async def _optional_api_key_user(
    api_key: Optional[str] = Security(api_key_header),
    db: AsyncSession = Depends(get_db),
) -> Optional[User]:
    if not api_key:
        return None
    key_hash = hash_api_key(api_key)
    result = await db.execute(
        select(APIKey).where(APIKey.key_hash == key_hash, APIKey.is_active == True)
    )
    api_key_record = result.scalar_one_or_none()
    if not api_key_record:
        return None

    api_key_record.last_used = datetime.now(timezone.utc)
    api_key_record.requests_count = (api_key_record.requests_count or 0) + 1

    result = await db.execute(
        select(User).where(User.id == api_key_record.user_id, User.is_active == True)
    )
    return result.scalar_one_or_none()


async def get_current_user(
    jwt_user: Optional[User] = Depends(_optional_jwt_user),
    api_key_user: Optional[User] = Depends(_optional_api_key_user),
) -> User:
    """Accept either Bearer JWT or X-API-Key header."""
    user = jwt_user or api_key_user
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required (Bearer token or X-API-Key)")
    return user


async def get_openai_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme),
    api_key: Optional[str] = Security(api_key_header),
    db: AsyncSession = Depends(get_db),
) -> User:
    """OpenAI clients send their key as a Bearer token, so `Authorization: Bearer
    mp_...` means an API key here. X-API-Key and dashboard JWTs also work."""
    token = credentials.credentials if credentials else api_key
    if not token:
        raise HTTPException(status_code=401, detail="Missing API key. Send it as: Authorization: Bearer mp_...")
    if not token.startswith(settings.API_KEY_PREFIX):
        return await get_current_user_jwt(await get_user_with_any_password(credentials, db))

    result = await db.execute(
        select(APIKey).where(APIKey.key_hash == hash_api_key(token), APIKey.is_active == True)
    )
    record = result.scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if not record or (record.expires_at and record.expires_at <= now):
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")
    record.last_used = now
    record.requests_count = (record.requests_count or 0) + 1

    result = await db.execute(select(User).where(User.id == record.user_id, User.is_active == True))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")
    request.state.api_key_prefix = record.key_prefix
    return user


def require_role(*roles: UserRole):
    """RBAC dependency factory."""
    async def _check(user: User = Depends(get_current_user_jwt)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail=f"Role {user.role} not permitted")
        return user
    return _check


require_admin = require_role(UserRole.ADMIN)


StatsScope = Literal["mine", "company"]


def stats_filter(column, scope: StatsScope, user: User) -> list:
    """Query conditions for usage numbers. Only admins can see the whole company's."""
    if scope == "mine":
        return [column == user.id]
    if user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Only admins can see company-wide numbers")
    return []
