"""
MeshPilot Database — SQLAlchemy async, PostgreSQL primary datastore.
All tables defined here; Alembic (backend/alembic/) handles migrations.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Enum, Float, ForeignKey,
    Integer, String, Text, JSON, Index,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, relationship

from core.config import settings


# ── Engine & session factory ──────────────────────────────────────────────────

# check_same_thread is a SQLite/aiosqlite-only requirement (invalid for
# asyncpg) — kept only for local dev/test convenience if DATABASE_URL is ever
# pointed back at SQLite; the deployed default is PostgreSQL, which needs no
# extra connect_args.
_connect_args = {"check_same_thread": False} if settings.DATABASE_URL.startswith("sqlite") else {}

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    connect_args=_connect_args,
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


class InstanceStatus(str, enum.Enum):
    STARTING = "starting"
    READY    = "ready"
    STOPPING = "stopping"
    ERROR    = "error"


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
    must_change_password = Column(Boolean, default=False, nullable=False)
    password_changed_at  = Column(DateTime(timezone=True), nullable=True)

    api_keys     = relationship("APIKey",    back_populates="user", cascade="all, delete-orphan")
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


class ModelInstance(Base):
    """Tracks which ModelRecord is actually loaded in a live llama.cpp
    container right now — the missing link that let two models both show
    status=READY in model_records while only one was truly being served
    (llama-server is single-model-per-process; see infra/llamacpp/start.sh).
    One row per model_id: its presence/status here, not model_records.status,
    is the source of truth for whether a model is currently servable."""
    __tablename__ = "model_instances"

    id             = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    model_id       = Column(String(36), ForeignKey("model_records.id"), nullable=False, unique=True)
    container_name = Column(String(128), nullable=False)
    endpoint_url   = Column(String(255), nullable=False)
    status         = Column(Enum(InstanceStatus), default=InstanceStatus.STARTING, nullable=False)
    created_at     = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    last_used_at   = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    model = relationship("ModelRecord")

    __table_args__ = (
        Index("ix_model_instance_last_used", "last_used_at"),
    )


class CompareRun(Base):
    """One successful 'with vs without MeshPilot' run. These rows are the
    measured source for the savings panel's speed, memory and cost figures."""
    __tablename__ = "compare_runs"

    id                         = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id                    = Column(String(36), ForeignKey("users.id"), nullable=True)
    model_id                   = Column(String(36), ForeignKey("model_records.id"), nullable=False)
    mesh_prompt_tokens         = Column(Integer, nullable=False)
    mesh_completion_tokens     = Column(Integer, nullable=False)
    mesh_latency_ms            = Column(Float, nullable=False)
    mesh_memory_mb             = Column(Float, nullable=True)
    baseline_prompt_tokens     = Column(Integer, nullable=False)
    baseline_completion_tokens = Column(Integer, nullable=False)
    baseline_latency_ms        = Column(Float, nullable=False)
    baseline_memory_mb         = Column(Float, nullable=True)
    created_at                 = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_compare_run_user", "user_id"),
    )


class PrivacyPolicy(Base):
    """The single row of routing rules (id is always 1). See core/privacy.py."""
    __tablename__ = "privacy_policies"

    id                   = Column(Integer, primary_key=True, default=1)
    cloud_enabled        = Column(Boolean, nullable=False, default=False)
    on_sensitive         = Column(String(16), nullable=False, default="local")  # "local" | "block"
    blocked_terms        = Column(JSON, nullable=False, default=list)
    auto_cloud_min_chars = Column(Integer, nullable=True)   # null = `auto` always stays local
    updated_at           = Column(DateTime(timezone=True), nullable=True)
    updated_by           = Column(String(255), nullable=True)


class AuditEvent(Base):
    """Append-only, hash-chained record of who did what and where data went.
    Written only through core/audit.py; a database trigger refuses changes."""
    __tablename__ = "audit_events"

    seq                = Column(BigInteger, primary_key=True, autoincrement=True)
    created_at         = Column(DateTime(timezone=True), nullable=False)
    event_type         = Column(String(64), nullable=False)
    actor_user_id      = Column(String(36), nullable=True)   # no foreign key: outlives the user
    actor_email        = Column(String(255), nullable=True)
    actor_key_prefix   = Column(String(16), nullable=True)
    request_id         = Column(String(36), nullable=True)
    model_id           = Column(String(64), nullable=True)
    route              = Column(String(16), nullable=True)   # local | cloud | blocked
    destination        = Column(String(255), nullable=True)
    reason             = Column(Text, nullable=True)
    sensitive          = Column(JSON, nullable=True)         # {category: count}, never the values
    prompt_fingerprint = Column(String(64), nullable=True)
    prompt_tokens      = Column(Integer, nullable=True)
    completion_tokens  = Column(Integer, nullable=True)
    status             = Column(String(32), nullable=True)
    detail             = Column(JSON, nullable=True)
    prev_hash          = Column(String(64), nullable=False)
    hash               = Column(String(64), nullable=False)

    __table_args__ = (
        Index("ix_audit_created", "created_at"),
        Index("ix_audit_actor", "actor_user_id"),
        Index("ix_audit_route", "route"),
    )


class FitRun(Base):
    """One fit report: the customer's test cases and the results on each model."""
    __tablename__ = "fit_runs"

    id           = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id      = Column(String(36), ForeignKey("users.id"), nullable=True)
    name         = Column(String(200), nullable=False)
    status       = Column(String(16), nullable=False, default="pending")
    suite        = Column(JSON, nullable=False)
    model_ids    = Column(JSON, nullable=False)
    progress     = Column(JSON, nullable=True)
    report       = Column(JSON, nullable=True)
    error        = Column(Text, nullable=True)
    created_at   = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    completed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_fit_run_user", "user_id"),
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
