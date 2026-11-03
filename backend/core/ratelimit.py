"""Fixed-window rate limits backed by Redis, shared across API workers.

If Redis is unreachable these allow the request and log it: Redis is on the
private network, and an outage shouldn't lock every user out.
"""

from __future__ import annotations

import redis.asyncio as redis
import structlog
from fastapi import HTTPException

from core.config import settings
from core.database import User, UserTier

logger = structlog.get_logger()

_client = redis.from_url(settings.REDIS_URL, socket_timeout=1, socket_connect_timeout=1)

LOGIN_FAILURES_PER_IP = 30


async def _hit(key: str, window_s: int) -> int:
    pipe = _client.pipeline()
    pipe.incr(key)
    pipe.expire(key, window_s, nx=True)
    count, _ = await pipe.execute()
    return count


async def _too_many_login_failures(keys: dict) -> bool:
    values = await _client.mget(list(keys))
    return any(v is not None and int(v) >= limit for v, limit in zip(values, keys.values()))


def _login_keys(email: str, ip: str) -> dict:
    return {
        f"login:fail:{email.lower()}:{ip}": settings.LOGIN_MAX_FAILURES,
        f"login:fail:ip:{ip}": LOGIN_FAILURES_PER_IP,
    }


async def check_login_allowed(email: str, ip: str) -> None:
    try:
        blocked = await _too_many_login_failures(_login_keys(email, ip))
    except redis.RedisError as e:
        logger.warning("ratelimit.unavailable", error=str(e))
        return
    if blocked:
        raise HTTPException(429, "Too many failed login attempts. Try again later.",
                            headers={"Retry-After": str(settings.LOGIN_WINDOW_S)})


async def record_login_failure(email: str, ip: str) -> None:
    try:
        for key in _login_keys(email, ip):
            await _hit(key, settings.LOGIN_WINDOW_S)
    except redis.RedisError as e:
        logger.warning("ratelimit.unavailable", error=str(e))


async def clear_login_failures(email: str, ip: str) -> None:
    try:
        await _client.delete(f"login:fail:{email.lower()}:{ip}")
    except redis.RedisError as e:
        logger.warning("ratelimit.unavailable", error=str(e))


def _rpm_limit(tier: UserTier):
    return {
        UserTier.FREE: settings.FREE_TIER_RPM,
        UserTier.PRO:  settings.PRO_TIER_RPM,
        UserTier.TEAM: settings.TEAM_TIER_RPM,
    }.get(tier)  # None = no limit (enterprise)


async def enforce_user_rpm(user: User) -> None:
    limit = _rpm_limit(user.tier)
    if limit is None:
        return
    try:
        count = await _hit(f"rpm:{user.id}", 60)
    except redis.RedisError as e:
        logger.warning("ratelimit.unavailable", error=str(e))
        return
    if count > limit:
        raise HTTPException(429, f"Rate limit of {limit} requests per minute exceeded for the {user.tier.value} tier.",
                            headers={"Retry-After": "60"})
