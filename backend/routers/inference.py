"""
MeshPilot Inference Router

Endpoints:
  POST /api/v1/inference/sync/{model_id}   — synchronous (waits for result)
  POST /api/v1/inference/async/{model_id}  — async (returns task_id immediately)
  GET  /api/v1/inference/tasks/{task_id}   — poll async task status
  GET  /api/v1/inference/history           — user's inference history
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database import (
    InferenceLog, InferenceStatus, ModelRecord, ModelStatus, User, get_db,
)
from core.security import get_current_user_api_key, get_current_user_jwt, get_current_user
from core.metrics import (
    INFERENCE_REQUESTS_TOTAL, INFERENCE_LATENCY_MS,
    INFERENCE_TTFT_MS, TOKENS_GENERATED_TOTAL, THROUGHPUT_TPS,
)

router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────────────

class InferenceRequest(BaseModel):
    prompt:       str
    max_tokens:   int   = Field(default=512, ge=1, le=4096)
    temperature:  float = Field(default=0.7, ge=0.0, le=2.0)
    top_p:        float = Field(default=0.9, ge=0.0, le=1.0)
    stop:         Optional[List[str]] = None
    stream:       bool  = False
    webhook_url:  Optional[str] = None   # for async callbacks


class InferenceResponse(BaseModel):
    id:               str
    model_id:         str
    status:           str
    text:             Optional[str]
    prompt_tokens:    Optional[int]
    completion_tokens:Optional[int]
    latency_ms:       Optional[float]
    ttft_ms:          Optional[float]
    throughput_tps:   Optional[float]
    backend_used:     Optional[str]
    created_at:       datetime


class AsyncTaskResponse(BaseModel):
    task_id:  str
    status:   str = "pending"
    poll_url: str


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_ready_model(model_id: str, user: User, db: AsyncSession) -> ModelRecord:
    result = await db.execute(select(ModelRecord).where(ModelRecord.id == model_id))
    m = result.scalar_one_or_none()
    if not m:
        raise HTTPException(404, "Model not found")
    if not m.is_public and m.owner_id != user.id:
        raise HTTPException(403, "Access denied")
    if m.status != ModelStatus.READY:
        raise HTTPException(409, f"Model is not ready (status: {m.status.value})")
    return m


async def _call_llamacpp(model: ModelRecord, req: InferenceRequest) -> dict:
    """Call the llama.cpp server OpenAI-compatible API."""
    payload = {
        "model":       model.file_path,
        "prompt":      req.prompt,
        "n_predict":   req.max_tokens,
        "temperature": req.temperature,
        "top_p":       req.top_p,
        "stop":        req.stop or [],
    }
    async with httpx.AsyncClient(timeout=settings.INFERENCE_TIMEOUT_S) as client:
        t0 = time.perf_counter()
        resp = await client.post(f"{settings.LLAMACPP_URL}/completion", json=payload)
        resp.raise_for_status()
        latency_ms = (time.perf_counter() - t0) * 1000
        data = resp.json()
        tokens_predicted = data.get("tokens_predicted", 0)
        tokens_evaluated  = data.get("tokens_evaluated", 0)
        tps = tokens_predicted / (latency_ms / 1000) if latency_ms > 0 else 0
        return {
            "text":              data.get("content", ""),
            "prompt_tokens":     tokens_evaluated,
            "completion_tokens": tokens_predicted,
            "latency_ms":        round(latency_ms, 1),
            "ttft_ms":           round(latency_ms * 0.15, 1),  # estimated
            "throughput_tps":    round(tps, 2),
            "backend_used":      "llamacpp",
        }


async def _call_onnx(model: ModelRecord, req: InferenceRequest) -> dict:
    """Call the ONNX Runtime inference server."""
    payload = {
        "model_path":  model.file_path,
        "prompt":      req.prompt,
        "max_tokens":  req.max_tokens,
        "temperature": req.temperature,
    }
    async with httpx.AsyncClient(timeout=settings.INFERENCE_TIMEOUT_S) as client:
        t0 = time.perf_counter()
        resp = await client.post(f"{settings.ONNX_URL}/infer", json=payload)
        resp.raise_for_status()
        latency_ms = (time.perf_counter() - t0) * 1000
        data = resp.json()
        tokens = data.get("tokens_generated", 0)
        tps = tokens / (latency_ms / 1000) if latency_ms > 0 else 0
        return {
            "text":              data.get("text", ""),
            "prompt_tokens":     data.get("prompt_tokens", 0),
            "completion_tokens": tokens,
            "latency_ms":        round(latency_ms, 1),
            "ttft_ms":           round(latency_ms * 0.12, 1),
            "throughput_tps":    round(tps, 2),
            "backend_used":      "onnx",
        }


async def _run_inference(model: ModelRecord, req: InferenceRequest) -> dict:
    backend = model.backend or "llamacpp"
    if "onnx" in backend:
        return await _call_onnx(model, req)
    return await _call_llamacpp(model, req)


# ── Sync endpoint ─────────────────────────────────────────────────────────────

@router.post("/sync/{model_id}", response_model=InferenceResponse)
async def infer_sync(
    model_id: str,
    req:      InferenceRequest,
    db:       AsyncSession = Depends(get_db),
    user:     User         = Depends(get_current_user_api_key),
):
    model = await _get_ready_model(model_id, user, db)

    log = InferenceLog(
        id=str(uuid.uuid4()), user_id=user.id, model_id=model.id,
        status=InferenceStatus.RUNNING,
        webhook_url=req.webhook_url,
    )
    db.add(log)
    await db.flush()

    try:
        result = await _run_inference(model, req)
        log.status             = InferenceStatus.COMPLETED
        log.prompt_tokens      = result["prompt_tokens"]
        log.completion_tokens  = result["completion_tokens"]
        log.latency_ms         = result["latency_ms"]
        log.ttft_ms            = result["ttft_ms"]
        log.throughput_tps     = result["throughput_tps"]
        log.backend_used       = result["backend_used"]
        log.completed_at       = datetime.now(timezone.utc)

        # Metrics
        INFERENCE_REQUESTS_TOTAL.labels(model_id=model_id, backend=result["backend_used"], status="success").inc()
        INFERENCE_LATENCY_MS.labels(model_id=model_id, backend=result["backend_used"]).observe(result["latency_ms"])
        TOKENS_GENERATED_TOTAL.labels(model_id=model_id, backend=result["backend_used"]).inc(result["completion_tokens"])
        THROUGHPUT_TPS.labels(model_id=model_id, backend=result["backend_used"]).set(result["throughput_tps"])

        return InferenceResponse(
            id=log.id, model_id=model_id, status="completed",
            text=result["text"],
            prompt_tokens=result["prompt_tokens"],
            completion_tokens=result["completion_tokens"],
            latency_ms=result["latency_ms"],
            ttft_ms=result["ttft_ms"],
            throughput_tps=result["throughput_tps"],
            backend_used=result["backend_used"],
            created_at=log.created_at,
        )
    except Exception as e:
        log.status        = InferenceStatus.FAILED
        log.error_message = str(e)[:500]
        # BUGFIX: the log row was only flush()ed, and raising here sends get_db
        # down its rollback path — which discarded the whole INSERT, not just the
        # status update. Every failed inference vanished from /history. Commit
        # the failure record before raising.
        await db.commit()
        INFERENCE_REQUESTS_TOTAL.labels(model_id=model_id, backend="unknown", status="error").inc()
        raise HTTPException(502, f"Inference failed: {e}")


# ── Async endpoint ────────────────────────────────────────────────────────────

@router.post("/async/{model_id}", response_model=AsyncTaskResponse, status_code=202)
async def infer_async(
    model_id: str,
    req:      InferenceRequest,
    db:       AsyncSession = Depends(get_db),
    user:     User         = Depends(get_current_user_api_key),
):
    model = await _get_ready_model(model_id, user, db)

    log = InferenceLog(
        id=str(uuid.uuid4()), user_id=user.id, model_id=model.id,
        status=InferenceStatus.PENDING, webhook_url=req.webhook_url,
    )
    db.add(log)
    # BUGFIX: was flush() — get_db only commits after the response is returned,
    # so Celery could pick up the task before the row was visible. The worker
    # then found no log and silently dropped the job, leaving the task PENDING
    # forever. Commit before dispatching so the row exists first.
    await db.commit()
    await db.refresh(log)

    # Dispatch to Celery
    from workers.inference_worker import run_inference_task
    run_inference_task.apply_async(
        args=[log.id, model_id, req.model_dump()],
        task_id=log.id,
        queue="inference",
    )

    return AsyncTaskResponse(
        task_id=log.id,
        status="pending",
        poll_url=f"/api/v1/inference/tasks/{log.id}",
    )


@router.get("/tasks/{task_id}", response_model=InferenceResponse)
async def get_task(
    task_id: str,
    db:      AsyncSession = Depends(get_db),
    user:    User         = Depends(get_current_user_api_key),
):
    result = await db.execute(
        select(InferenceLog).where(InferenceLog.id == task_id, InferenceLog.user_id == user.id)
    )
    log = result.scalar_one_or_none()
    if not log:
        raise HTTPException(404, "Task not found")
    return InferenceResponse(
        id=log.id, model_id=log.model_id, status=log.status.value,
        text=None, prompt_tokens=log.prompt_tokens,
        completion_tokens=log.completion_tokens, latency_ms=log.latency_ms,
        ttft_ms=log.ttft_ms, throughput_tps=log.throughput_tps,
        backend_used=log.backend_used, created_at=log.created_at,
    )


@router.get("/history", response_model=List[InferenceResponse])
async def inference_history(
    limit:  int = 50,
    db:     AsyncSession = Depends(get_db),
    user:   User         = Depends(get_current_user),
):
    result = await db.execute(
        select(InferenceLog)
        .where(InferenceLog.user_id == user.id)
        .order_by(desc(InferenceLog.created_at))
        .limit(min(limit, 200))
    )
    logs = result.scalars().all()
    return [InferenceResponse(
        id=l.id, model_id=l.model_id, status=l.status.value,
        text=None, prompt_tokens=l.prompt_tokens,
        completion_tokens=l.completion_tokens, latency_ms=l.latency_ms,
        ttft_ms=l.ttft_ms, throughput_tps=l.throughput_tps,
        backend_used=l.backend_used, created_at=l.created_at,
    ) for l in logs]
