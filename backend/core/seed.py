"""Seed the database with the pre-loaded demo model record and bootstrap admin."""

from __future__ import annotations

import logging
import re
from sqlalchemy import select
from core.config import settings
from core.database import (
    AsyncSessionLocal, ModelFormat, ModelRecord, ModelStatus, QuantBits,
    User, UserRole, UserTier,
)
from core.security import hash_password

logger = logging.getLogger("meshpilot.seed")

DEMO_MODELS = [
    {
        "id":            "demo-llama-1b",
        "name":          "Llama-3.2-1B-Instruct (Q4_K_M)",
        "slug":          "llama-3.2-1b-instruct-q4",
        "description":   "Meta Llama 3.2 1B Instruct — INT4 quantized GGUF. Pre-loaded for instant demo. Runs on any 4-core CPU with 4GB RAM.",
        "format":        ModelFormat.GGUF,
        "status":        ModelStatus.READY,
        "quant_bits":    QuantBits.INT4,
        "file_path":     "/models/Llama-3.2-1B-Instruct-Q4_K_M.gguf",
        "context_length":8192,
        "parameters_b":  1.24,
        "backend":       "llamacpp",
        "is_public":     True,
        "metadata_":     {
            "hf_repo":   "bartowski/Llama-3.2-1B-Instruct-GGUF",
            "license":   "Llama 3.2 Community License",
            "task":      "text-generation",
        },
    },
]


async def seed_demo_models() -> None:
    async with AsyncSessionLocal() as session:
        for model_data in DEMO_MODELS:
            result = await session.execute(
                select(ModelRecord).where(ModelRecord.id == model_data["id"])
            )
            existing = result.scalar_one_or_none()
            if existing:
                continue

            record = ModelRecord(**model_data)
            session.add(record)
            logger.info(f"Seeded demo model: {model_data['slug']}")

        await session.commit()


async def seed_admin_user() -> None:
    """Create the admin account from ADMIN_EMAIL/ADMIN_PASSWORD on first startup, if configured."""
    if not settings.ADMIN_EMAIL or not settings.ADMIN_PASSWORD:
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.email == settings.ADMIN_EMAIL))
        if result.scalar_one_or_none():
            return

        username = re.sub(r"[^a-z0-9_-]", "", settings.ADMIN_EMAIL.split("@")[0].lower())[:32]
        if len(username) < 3:
            username = "admin"
        result = await session.execute(select(User).where(User.username == username))
        if result.scalar_one_or_none():
            username = f"{username}-admin"[:32]

        user = User(
            email=settings.ADMIN_EMAIL,
            username=username,
            hashed_password=hash_password(settings.ADMIN_PASSWORD),
            role=UserRole.ADMIN,
            tier=UserTier.ENTERPRISE,
        )
        session.add(user)
        await session.commit()
        logger.info(f"Seeded admin user: {settings.ADMIN_EMAIL}")
