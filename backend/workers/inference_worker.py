"""Celery worker — async inference task."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import httpx
from core.celery_app import celery_app
from core.config import settings


@celery_app.task(name="workers.inference_worker.run_inference_task", bind=True, max_retries=2)
def run_inference_task(self, log_id: str, model_id: str, req_data: dict, audit_context: dict | None = None):
    """Run inference asynchronously and update the InferenceLog record."""
    asyncio.run(_async_run_and_clean_up(log_id, model_id, req_data, audit_context))


async def _async_run_and_clean_up(log_id: str, model_id: str, req_data: dict, audit_context: dict | None):
    from core.database import engine
    try:
        await _async_run(log_id, model_id, req_data, audit_context)
    finally:
        # asyncio.run gives each task a new event loop; pooled connections can't cross loops.
        await engine.dispose()


async def _async_run(log_id: str, model_id: str, req_data: dict, audit_context: dict | None):
    from core import audit
    from core.database import AsyncSessionLocal, InferenceLog, InferenceStatus, ModelRecord, ModelStatus
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        # Load log and model
        log_res = await db.execute(select(InferenceLog).where(InferenceLog.id == log_id))
        log = log_res.scalar_one_or_none()
        if not log:
            return

        model_res = await db.execute(select(ModelRecord).where(ModelRecord.id == model_id))
        model = model_res.scalar_one_or_none()
        if not model or model.status != ModelStatus.READY:
            log.status = InferenceStatus.FAILED
            log.error_message = "Model not ready"
            await db.commit()
            if audit_context:
                await audit.record_event("inference.failed", **audit_context, status="failed", reason="Model not ready")
            return

        log.status = InferenceStatus.RUNNING
        await db.commit()

        try:
            backend = model.backend or "llamacpp"
            t0 = time.perf_counter()

            if "onnx" in backend:
                async with httpx.AsyncClient(timeout=settings.INFERENCE_TIMEOUT_S) as client:
                    resp = await client.post(f"{settings.ONNX_URL}/infer", json={
                        "model_path": model.file_path,
                        "prompt": req_data["prompt"],
                        "max_tokens": req_data.get("max_tokens", 512),
                        "temperature": req_data.get("temperature", 0.7),
                    })
                    resp.raise_for_status()
                    data = resp.json()
                    text = data.get("text", "")
                    tokens = data.get("tokens_generated", 0)
            else:
                # llama-server is single-model-per-process -- ensure a dynamic
                # instance for THIS model is running before calling it (see
                # core/model_orchestrator.py; mirrors routers/inference.py's
                # _call_llamacpp, which the sync/compare endpoints use).
                from core.model_orchestrator import ensure_model_running, fit_to_context
                endpoint = await ensure_model_running(model)
                async with httpx.AsyncClient(timeout=settings.INFERENCE_TIMEOUT_S) as client:
                    resp = await client.post(f"{endpoint}/completion", json={
                        "model": model.file_path,
                        "prompt": req_data["prompt"],
                        "n_predict": await fit_to_context(endpoint, req_data["prompt"], req_data.get("max_tokens", 512)),
                        "temperature": req_data.get("temperature", 0.7),
                        "top_p": req_data.get("top_p", 0.9),
                        "stop": req_data.get("stop", []),
                    })
                    resp.raise_for_status()
                    data = resp.json()
                    text = data.get("content", "")
                    tokens = data.get("tokens_predicted", 0)

            latency_ms = (time.perf_counter() - t0) * 1000
            tps = tokens / (latency_ms / 1000) if latency_ms > 0 else 0

            log.status             = InferenceStatus.COMPLETED
            log.completion_tokens  = tokens
            log.latency_ms         = round(latency_ms, 1)
            log.throughput_tps     = round(tps, 2)
            log.backend_used       = backend
            log.completed_at       = datetime.now(timezone.utc)

            # Fire webhook if configured
            if log.webhook_url:
                await _fire_webhook(log.webhook_url, {
                    "task_id": log_id, "status": "completed",
                    "text": text, "tokens": tokens, "latency_ms": latency_ms,
                })
                log.webhook_sent = True

        except Exception as e:
            log.status        = InferenceStatus.FAILED
            log.error_message = str(e)[:500]

        await db.commit()
        if audit_context:
            completed = log.status == InferenceStatus.COMPLETED
            await audit.record_event(
                "inference.completed" if completed else "inference.failed", **audit_context,
                status="completed" if completed else "failed",
                completion_tokens=log.completion_tokens,
                reason=None if completed else (log.error_message or "")[:200],
            )


async def _fire_webhook(url: str, payload: dict):
    from core.net_safety import ensure_public_url
    try:
        # Checked again here: DNS can change after the request was accepted.
        await ensure_public_url(url)
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json=payload)
    except Exception:
        pass  # Webhook delivery failures are non-fatal
