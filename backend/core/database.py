"""
MeshPilot Database — SQLAlchemy async with SQLite.
All tables defined here; Alembic handles migrations.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean, Column, DateTime, Enum, Float, ForeignKey,
    Integer, String, Text, JSON, Index,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, relationship

from core.config import settings


# ── Engine & session factory ──────────────────────────────────────────────────

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False},
)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ── Base ──────────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


# ── Enumerations ──────────────────────────────────────────────────────────────

class UserRole(str, enum.Enum):
    ADMIN = "admin"
    USER  = "user"


class UserTier(str, enum.Enum):
    FREE       = "free"
    PRO        = "pro"
    TEAM       = "team"
    ENTERPRISE = "enterprise"


class ModelFormat(str, enum.Enum):
    GGUF    = "gguf"
    ONNX    = "onnx"
    PYTORCH = "pytorch"


class ModelStatus(str, enum.Enum):
    UPLOADING   = "uploading"
    QUANTIZING  = "quantizing"
    READY       = "ready"
    ERROR       = "error"


class InferenceStatus(str, enum.Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    COMPLETED = "completed"
    FAILED    = "failed"


class QuantBits(str, enum.Enum):
    INT4 = "int4"
    INT8 = "int8"
    FP16 = "fp16"
    FP32 = "fp32"


# ── ORM Models ────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id            = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email         = Column(String(255), unique=True, nullable=False, index=True)
    username      = Column(String(64),  unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    role          = Column(Enum(UserRole), default=UserRole.USER, nullable=False)
    tier          = Column(Enum(UserTier), default=UserTier.FREE, nullable=False)
    is_active     = Column(Boolean, default=True)
    created_at    = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    last_login    = Column(DateTime(timezone=True), nullable=True)

    api_keys      = relationship("APIKey",    back_populates="user", cascade="all, delete-orphan")
    models        = relationship("ModelRecord", back_populates="owner")
    inferences    = relationship("InferenceLog", back_populates="user")
    webhooks      = relationship("Webhook",   back_populates="user", cascade="all, delete-orphan")


class APIKey(Base):
    __tablename__ = "api_keys"

    id         = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id    = Column(String(36), ForeignKey("users.id"), nullable=False)
    key_hash   = Column(String(255), unique=True, nullable=False, index=True)
    key_prefix = Column(String(16), nullable=False)   # first 8 chars for display
    name       = Column(String(128), nullable=False, default="Default Key")
    is_active  = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    last_used  = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    requests_count = Column(Integer, default=0)

    user = relationship("User", back_populates="api_keys")


class ModelRecord(Base):
    __tablename__ = "model_records"

    id             = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id       = Column(String(36), ForeignKey("users.id"), nullable=True)  # null = system model
    name           = Column(String(128), nullable=False)
    slug           = Column(String(128), unique=True, nullable=False, index=True)
    description    = Column(Text, nullable=True)
    format         = Column(Enum(ModelFormat), nullable=False)
    status         = Column(Enum(ModelStatus), default=ModelStatus.UPLOADING)
    quant_bits     = Column(Enum(QuantBits), nullable=True)
    file_path      = Column(String(512), nullable=True)
    file_size_bytes= Column(Integer, nullable=True)
    context_length = Column(Integer, default=4096)
    parameters_b   = Column(Float, nullable=True)   # billions of parameters
    backend        = Column(String(32), nullable=True)  # "llamacpp" | "onnx"
    cpu_features   = Column(JSON, nullable=True)     # detected CPU capabilities
    metadata_      = Column("metadata", JSON, nullable=True)
    is_public      = Column(Boolean, default=False)
    created_at     = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at     = Column(DateTime(timezone=True), onupdate=lambda: datetime.now(timezone.utc))

    owner      = relationship("User", back_populates="models")
    inferences = relationship("InferenceLog", back_populates="model")

    __table_args__ = (
        Index("ix_model_status", "status"),
        Index("ix_model_owner", "owner_id"),
    )


class InferenceLog(Base):
    __tablename__ = "inference_logs"

    id             = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id        = Column(String(36), ForeignKey("users.id"), nullable=True)
    model_id       = Column(String(36), ForeignKey("model_records.id"), nullable=False)
    status         = Column(Enum(InferenceStatus), default=InferenceStatus.PENDING)
    prompt_tokens  = Column(Integer, nullable=True)
    completion_tokens = Column(Integer, nullable=True)
    latency_ms     = Column(Float, nullable=True)
    ttft_ms        = Column(Float, nullable=True)   # time-to-first-token
    throughput_tps = Column(Float, nullable=True)   # tokens per second
    backend_used   = Column(String(32), nullable=True)
    error_message  = Column(Text, nullable=True)
    webhook_url    = Column(String(512), nullable=True)
    webhook_sent   = Column(Boolean, default=False)
    created_at     = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    completed_at   = Column(DateTime(timezone=True), nullable=True)

    user  = relationship("User",        back_populates="inferences")
    model = relationship("ModelRecord", back_populates="inferences")

    __table_args__ = (
        Index("ix_inference_user", "user_id"),
        Index("ix_inference_model", "model_id"),
        Index("ix_inference_created", "created_at"),
    )


class Webhook(Base):
    __tablename__ = "webhooks"

    id         = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id    = Column(String(36), ForeignKey("users.id"), nullable=False)
    url        = Column(String(512), nullable=False)
    secret     = Column(String(255), nullable=True)   # HMAC signing secret
    events     = Column(JSON, nullable=False, default=list)  # ["inference.complete", ...]
    is_active  = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="webhooks")
