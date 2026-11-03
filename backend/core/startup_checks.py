"""Refuses to start in production with placeholder or weak secrets."""

from __future__ import annotations

from typing import List

from sqlalchemy.engine import make_url

MIN_SECRET_LENGTH = 32
MIN_ADMIN_PASSWORD_LENGTH = 12


def _is_placeholder(value: str) -> bool:
    lowered = value.lower().replace("-", "").replace("_", "")
    return "changeme" in lowered


def find_insecure_settings(settings) -> List[str]:
    problems: List[str] = []

    if _is_placeholder(settings.SECRET_KEY) or len(settings.SECRET_KEY) < MIN_SECRET_LENGTH:
        problems.append(f"SECRET_KEY must be a random value of at least {MIN_SECRET_LENGTH} characters")

    if _is_placeholder(settings.AUDIT_HMAC_KEY) or len(settings.AUDIT_HMAC_KEY) < MIN_SECRET_LENGTH:
        problems.append(f"AUDIT_HMAC_KEY must be a random value of at least {MIN_SECRET_LENGTH} characters")

    db_password = make_url(settings.DATABASE_URL).password or ""
    if not settings.DATABASE_URL.startswith("sqlite") and (not db_password or _is_placeholder(db_password)):
        problems.append("the DATABASE_URL password is a placeholder")

    admin_password = settings.ADMIN_PASSWORD
    if admin_password and (_is_placeholder(admin_password) or len(admin_password) < MIN_ADMIN_PASSWORD_LENGTH):
        problems.append(f"ADMIN_PASSWORD must be at least {MIN_ADMIN_PASSWORD_LENGTH} characters and not a placeholder")

    if settings.CLOUD_ROUTING_ENABLED:
        if not (settings.CLOUD_API_BASE and settings.CLOUD_API_KEY and settings.CLOUD_MODEL):
            problems.append("CLOUD_ROUTING_ENABLED needs CLOUD_API_BASE, CLOUD_API_KEY and CLOUD_MODEL")
        elif not settings.CLOUD_API_BASE.startswith("https://"):
            problems.append("CLOUD_API_BASE must use https://")

    return problems


def enforce_secure_settings(settings, logger) -> None:
    problems = find_insecure_settings(settings)
    if not problems:
        return
    if settings.MESHPILOT_ENV == "development":
        for problem in problems:
            logger.warning("meshpilot.insecure_setting", problem=problem)
        return
    raise RuntimeError(
        "Refusing to start with insecure settings: " + "; ".join(problems)
        + ". Fix them in .env, or set MESHPILOT_ENV=development for local testing only."
    )
