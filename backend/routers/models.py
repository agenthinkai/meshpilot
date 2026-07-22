"""MeshPilot Model Registry Router — upload, list, delete, status."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import aiofiles
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database import ModelFormat, ModelRecord, ModelStatus, QuantBits, User, get_db
from core.security import get_current_user_api_key, get_current_user_jwt
from core.cpu_detect import get_cpu_profile
from core.metrics import MODEL_UPLOAD_TOTAL, MODELS_LOADED

router = APIRouter()

MAX_BYTES = int(settings.MAX_MODEL_SIZE_GB * 1024 ** 3)

FORMAT_MAP = {
    ".gguf":        ModelFormat.GGUF,
    ".onnx":        ModelFormat.ONNX,
    ".pt":          ModelFormat.PYTORCH,
    ".pth":         ModelFormat.PYTORCH,
    ".bin":         ModelFormat.PYTORCH,
    ".safetensors": ModelFormat.PYTORCH,
}


class ModelOut(BaseModel):
    id:             str
    name:           str
    slug:           str
    description:    Optional[str]
    format:         str
    status:         str
    quant_bits:     Optional[str]
    file_size_bytes:Optional[int]
    context_length: int
    parameters_b:   Optional[float]
    backend:        Optional[str]
    is_public:      bool
    created_at:     datetime


def _model_to_out(m: ModelRecord) -> ModelOut:
    return ModelOut(
        id=m.id, name=m.name, slug=m.slug, description=m.description,
        format=m.format.value, status=m.status.value,
        quant_bits=m.quant_bits.value if m.quant_bits else None,
        file_size_bytes=m.file_size_bytes, context_length=m.context_length,
        parameters_b=m.parameters_b, backend=m.backend,
        is_public=m.is_public, created_at=m.created_at,
    )


@router.get("", response_model=List[ModelOut])
async def list_models(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_jwt),
):
    result = await db.execute(
        select(ModelRecord).where(
            (ModelRecord.owner_id == user.id) | (ModelRecord.is_public == True)
        )
    )
    return [_model_to_out(m) for m in result.scalars().all()]


@router.get("/{model_id}", response_model=ModelOut)
async def get_model(
    model_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_jwt),
):
    result = await db.execute(select(ModelRecord).where(ModelRecord.id == model_id))
    m = result.scalar_one_or_none()
    if not m:
        raise HTTPException(404, "Model not found")
    if not m.is_public and m.owner_id != user.id:
        raise HTTPException(403, "Access denied")
    return _model_to_out(m)


@router.post("", response_model=ModelOut, status_code=202)
async def upload_model(
    background_tasks: BackgroundTasks,
    file:        UploadFile = File(...),
    name:        str        = Form(...),
    description: str        = Form(""),
    db:          AsyncSession = Depends(get_db),
    user:        User         = Depends(get_current_user_jwt),
):
    # Tier model limits
    result = await db.execute(
        select(ModelRecord).where(ModelRecord.owner_id == user.id)
    )
    user_models = result.scalars().all()
    from core.database import UserTier
    limits = {UserTier.FREE: 1, UserTier.PRO: 5, UserTier.TEAM: 9999, UserTier.ENTERPRISE: 9999}
    if len(user_models) >= limits.get(user.tier, 1):
        raise HTTPException(429, f"Model limit reached for {user.tier.value} tier. Upgrade to add more models.")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in FORMAT_MAP:
        raise HTTPException(400, f"Unsupported format: {suffix}")

    model_id  = str(uuid.uuid4())
    slug      = f"{user.username}-{name.lower().replace(' ', '-')}-{model_id[:8]}"
    dest_dir  = Path(settings.MODEL_STORAGE) / user.id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"{model_id}{suffix}"

    # Stream upload to disk
    total = 0
    async with aiofiles.open(dest_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):  # 1MB chunks
            total += len(chunk)
            if total > MAX_BYTES:
                await f.close()
                dest_path.unlink(missing_ok=True)
                raise HTTPException(413, f"File exceeds {settings.MAX_MODEL_SIZE_GB}GB limit")
            await f.write(chunk)

    cpu = get_cpu_profile()
    fmt = FORMAT_MAP[suffix]

    record = ModelRecord(
        id=model_id, owner_id=user.id, name=name, slug=slug,
        description=description, format=fmt, status=ModelStatus.QUANTIZING,
        file_path=str(dest_path), file_size_bytes=total,
        cpu_features=cpu.to_dict(),
    )
    db.add(record)
    await db.flush()

    MODEL_UPLOAD_TOTAL.labels(format=fmt.value, status="accepted").inc()

    # Kick off background quantization
    background_tasks.add_task(_quantize_background, model_id, str(dest_path))

    return _model_to_out(record)


async def _quantize_background(model_id: str, file_path: str):
    """Run quantization in a background task and update model status."""
    from core.quantizer import AutoQuantizer
    from core.database import AsyncSessionLocal
    from sqlalchemy import select

    quantizer = AutoQuantizer(model_id, file_path)
    result = quantizer.run()

    async with AsyncSessionLocal() as db:
        res = await db.execute(select(ModelRecord).where(ModelRecord.id == model_id))
        record = res.scalar_one_or_none()
        if not record:
            return

        if result.success:
            record.status     = ModelStatus.READY
            record.file_path  = result.output_path
            record.quant_bits = QuantBits(result.quant_bits) if result.quant_bits else None
            cpu = get_cpu_profile()
            record.backend    = cpu.recommended_backend
        else:
            record.status = ModelStatus.ERROR
            record.metadata_ = {"error": result.error}

        await db.commit()
        MODELS_LOADED.inc()


@router.delete("/{model_id}", status_code=204)
async def delete_model(
    model_id: str,
    db:   AsyncSession = Depends(get_db),
    user: User         = Depends(get_current_user_jwt),
):
    result = await db.execute(select(ModelRecord).where(ModelRecord.id == model_id))
    m = result.scalar_one_or_none()
    if not m:
        raise HTTPException(404, "Model not found")
    if m.owner_id != user.id:
        raise HTTPException(403, "Access denied")

    if m.file_path and os.path.exists(m.file_path):
        os.unlink(m.file_path)

    await db.delete(m)
    MODELS_LOADED.dec()
