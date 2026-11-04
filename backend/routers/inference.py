"""
MeshPilot Inference Router

Endpoints:
  POST /api/v1/inference/sync/{model_id}   — synchronous (waits for result)
  POST /api/v1/inference/async/{model_id}  — async (returns task_id immediately)
  GET  /api/v1/inference/tasks/{task_id}   — poll async task status
  GET  /api/v1/inference/history           — user's inference history
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from core import audit
from core.config import settings
from core.database import (
    CompareRun, InferenceLog, InferenceStatus, ModelRecord, ModelStatus, PrivacyPolicy, User, get_db,
)
from core.model_orchestrator import container_memory_mb, container_name_for, ensure_model_running, fit_to_context
from core.net_safety import UnsafeURL, ensure_public_url
from core.privacy import scan_text
from core.security import StatsScope, get_current_user_api_key, get_current_user_jwt, get_current_user, stats_filter
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
    """Call the llama.cpp server OpenAI-compatible API. Ensures a dynamic
    per-model instance is running first — llama-server serves exactly one
    GGUF per process, so there is no single standing LLAMACPP_URL anymore."""
    endpoint = await ensure_model_running(model)
    payload = {
        "model":       model.file_path,
        "prompt":      req.prompt,
        "n_predict":   await fit_to_context(endpoint, req.prompt, req.max_tokens),
        "temperature": req.temperature,
        "top_p":       req.top_p,
        "stop":        req.stop or [],
    }
    async with httpx.AsyncClient(timeout=settings.INFERENCE_TIMEOUT_S) as client:
        t0 = time.perf_counter()
        resp = await client.post(f"{endpoint}/completion", json=payload)
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


# Models with both a llamacpp (INT4 GGUF) side AND a matching plain-PyTorch
# FP32 baseline deployed, keyed by model_id -> the chat prompt format. Compare
# only ever runs the SAME underlying model on both sides, and sends both the
# identical formatted prompt, so token counts and speeds stay comparable.
COMPARE_PROMPT_TEMPLATES = {
    "benchmark-qwen25-05b": (
        "<|im_start|>system\nYou are Qwen, created by Alibaba Cloud. You are a helpful assistant.<|im_end|>\n"
        "<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n"
    ),
}


async def _audit_routed(db: AsyncSession, user: User, model_id: str, prompt: str) -> dict:
    """Records a local request before it runs, and returns the fields for its
    outcome event. These endpoints never leave the server, so nothing is rerouted."""
    policy = (await db.execute(select(PrivacyPolicy).where(PrivacyPolicy.id == 1))).scalar_one()
    context = {
        "actor_user_id": user.id, "actor_email": user.email, "model_id": model_id,
        "route": "local", "destination": "local", "prompt_fingerprint": audit.prompt_fingerprint(prompt),
    }
    await audit.record_event(
        "inference.routed", **context, reason="local model requested", status="routed",
        sensitive=scan_text(prompt, policy.blocked_terms or []),
    )
    return context


async def _call_pytorch_baseline(req: InferenceRequest) -> dict:
    """Call the standing 'without MeshPilot' baseline server (plain PyTorch, FP32)."""
    payload = {"prompt": req.prompt, "max_tokens": req.max_tokens, "temperature": req.temperature, "raw_prompt": True}
    async with httpx.AsyncClient(timeout=settings.INFERENCE_TIMEOUT_S) as client:
        resp = await client.post(f"{settings.PYTORCH_BASELINE_URL}/generate", json=payload)
        resp.raise_for_status()
        return resp.json()


# ── Sync endpoint ─────────────────────────────────────────────────────────────

@router.post("/sync/{model_id}", response_model=InferenceResponse)
async def infer_sync(
    model_id: str,
    req:      InferenceRequest,
    db:       AsyncSession = Depends(get_db),
    user:     User         = Depends(get_current_user),
):
    model = await _get_ready_model(model_id, user, db)
    audit_context = await _audit_routed(db, user, model.id, req.prompt)

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

        await audit.record_event(
            "inference.completed", **audit_context, status="completed",
            prompt_tokens=result["prompt_tokens"], completion_tokens=result["completion_tokens"],
        )

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
        await audit.record_event("inference.failed", **audit_context, status="failed", reason=str(e)[:200])
        raise HTTPException(502, f"Inference failed: {e}")


# ── Compare endpoint ("with MeshPilot" vs "without MeshPilot") ────────────────

@router.post("/compare/{model_id}")
async def infer_compare(
    model_id: str,
    req:      InferenceRequest,
    db:       AsyncSession = Depends(get_db),
    user:     User         = Depends(get_current_user),
):
    """Runs the SAME model two ways and returns both: through MeshPilot
    (llama.cpp, INT4 GGUF) and through plain PyTorch (FP32, no MeshPilot).
    Both calls fire concurrently so neither side waits on the other. Temperature
    is forced to 0 because the baseline always generates greedily."""
    if model_id not in COMPARE_PROMPT_TEMPLATES:
        raise HTTPException(
            400,
            f"No 'without MeshPilot' baseline is deployed for this model yet. "
            f"Compare currently supports: {', '.join(sorted(COMPARE_PROMPT_TEMPLATES))}",
        )
    model = await _get_ready_model(model_id, user, db)
    audit_context = await _audit_routed(db, user, model.id, req.prompt)
    req = req.model_copy(update={
        "prompt": COMPARE_PROMPT_TEMPLATES[model_id].replace("{prompt}", req.prompt),
        "temperature": 0.0,
    })

    # Finish any cold start first, so the memory sample below finds a container
    # and both sides begin generating together. A failure here resurfaces as
    # the "with" side's error when _run_inference retries it.
    try:
        await ensure_model_running(model)
    except Exception:
        pass

    async def _with_meshpilot():
        t0 = time.perf_counter()
        try:
            result = await _run_inference(model, req)
            result["wall_clock_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            return result
        except Exception as e:
            return {"error": str(e)}

    async def _without_meshpilot():
        t0 = time.perf_counter()
        try:
            result = await _call_pytorch_baseline(req)
            result["wall_clock_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            return result
        except Exception as e:
            return {"error": str(e)}

    async def _sample_memory():
        # Give generation a moment to actually start before sampling, so this
        # reflects memory while the model is working, not idle baseline.
        await asyncio.sleep(0.4)
        return await asyncio.gather(
            container_memory_mb(container_name_for(model.id)),
            container_memory_mb("meshpilot-pytorch-baseline"),
        )

    (with_result, without_result), (with_mem_mb, without_mem_mb) = await asyncio.gather(
        asyncio.gather(_with_meshpilot(), _without_meshpilot()),
        _sample_memory(),
    )

    if with_mem_mb is not None:
        with_result["memory_mb"] = with_mem_mb
    if without_mem_mb is not None:
        without_result["memory_mb"] = without_mem_mb

    # Tokens per second, not total response time: the two sides can stop at
    # different token counts, which would otherwise distort the ratio.
    speedup = None
    if with_result.get("throughput_tps") and without_result.get("throughput_tps"):
        speedup = round(with_result["throughput_tps"] / without_result["throughput_tps"], 2)

    memory_reduction_pct = None
    if with_result.get("memory_mb") and without_result.get("memory_mb"):
        memory_reduction_pct = round(
            (1 - with_result["memory_mb"] / without_result["memory_mb"]) * 100, 1
        )

    if "error" not in with_result and "error" not in without_result:
        db.add(CompareRun(
            user_id=user.id, model_id=model_id,
            mesh_prompt_tokens=with_result["prompt_tokens"],
            mesh_completion_tokens=with_result["completion_tokens"],
            mesh_latency_ms=with_result["latency_ms"],
            mesh_memory_mb=with_mem_mb,
            baseline_prompt_tokens=without_result["prompt_tokens"],
            baseline_completion_tokens=without_result["completion_tokens"],
            baseline_latency_ms=without_result["latency_ms"],
            baseline_memory_mb=without_mem_mb,
        ))
        # Commit now: the page reloads its savings totals as soon as this returns.
        await db.commit()

    INFERENCE_REQUESTS_TOTAL.labels(model_id=model_id, backend="compare", status="success").inc()

    compare_ok = "error" not in with_result and "error" not in without_result
    await audit.record_event(
        "inference.completed" if compare_ok else "inference.failed", **audit_context,
        status="completed" if compare_ok else "failed",
        prompt_tokens=with_result.get("prompt_tokens"), completion_tokens=with_result.get("completion_tokens"),
        detail={"endpoint": "compare"},
    )

    return {
        "model_id": model_id,
        "with_meshpilot": with_result,
        "without_meshpilot": without_result,
        "speedup_x": speedup,
        "memory_reduction_pct": memory_reduction_pct,
    }


# ── Async endpoint ────────────────────────────────────────────────────────────

@router.post("/async/{model_id}", response_model=AsyncTaskResponse, status_code=202)
async def infer_async(
    model_id: str,
    req:      InferenceRequest,
    db:       AsyncSession = Depends(get_db),
    user:     User         = Depends(get_current_user_api_key),
):
    model = await _get_ready_model(model_id, user, db)
    if req.webhook_url:
        try:
            await ensure_public_url(req.webhook_url)
        except UnsafeURL as e:
            raise HTTPException(400, str(e))
    audit_context = await _audit_routed(db, user, model.id, req.prompt)

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
        args=[log.id, model_id, req.model_dump(), audit_context],
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
    scope:  StatsScope   = "mine",
    db:     AsyncSession = Depends(get_db),
    user:   User         = Depends(get_current_user),
):
    result = await db.execute(
        select(InferenceLog)
        .where(*stats_filter(InferenceLog.user_id, scope, user))
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
