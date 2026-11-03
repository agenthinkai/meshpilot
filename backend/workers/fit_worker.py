"""Celery worker — runs a fit report's test cases against each local model."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

import httpx

from core.celery_app import celery_app
from core.config import settings

THREADS_PER_MODEL = 4


@celery_app.task(
    name="workers.fit_worker.run_fit_task",
    # Long runs would outlast Redis's visibility timeout and be delivered twice.
    acks_late=False,
    soft_time_limit=settings.FIT_TIMEOUT_S,
    time_limit=settings.FIT_TIMEOUT_S + 60,
)
def run_fit_task(run_id: str):
    asyncio.run(_run(run_id))


async def _generate(client: httpx.AsyncClient, endpoint: str, prompt: str, max_tokens: int) -> dict:
    """Streams, because this llama-server build returns no timings otherwise.
    Time to the first token is the prompt phase; the remainder is generation."""
    started = time.perf_counter()
    first_token_at = None
    parts, usage = [], {}
    async with client.stream("POST", f"{endpoint}/v1/chat/completions", json={
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "seed": 42,
        "cache_prompt": False,
        "stream": True,
    }) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                continue
            chunk = json.loads(line[5:])
            usage = chunk.get("usage") or usage
            for choice in chunk.get("choices") or []:
                content = (choice.get("delta") or {}).get("content")
                if content:
                    first_token_at = first_token_at or time.perf_counter()
                    parts.append(content)
    finished = time.perf_counter()
    first_token_at = first_token_at or finished
    return {
        "output": "".join(parts).strip(),
        "latency_s": round(finished - started, 2),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "prompt_s": round(first_token_at - started, 3),
        "generation_s": round(finished - first_token_at, 3),
    }


async def _run_case(client: httpx.AsyncClient, endpoint: str, case: dict, index: int) -> dict:
    from core.fit import score_case
    from core.model_orchestrator import PromptTooLong, fit_to_context

    row = {"index": index, "task": case["task"], "prompt_preview": case["prompt"][:160]}
    failure = {"output": "", "score": 0, "passed": False, "latency_s": 0}
    max_tokens = case.get("max_tokens", 200)
    try:
        allowed = await fit_to_context(endpoint, case["prompt"], max_tokens)
        result = await _generate(client, endpoint, case["prompt"], allowed)
    except PromptTooLong as e:
        return {**row, **failure, "missed": ["document too long for this model"], "error": str(e)}
    except Exception as e:
        return {**row, **failure, "missed": ["request failed"], "error": str(e)[:300]}
    return {**row, **result, **score_case(result["output"], case["expect"])}


async def _run(run_id: str) -> None:
    from sqlalchemy import select

    from core import audit
    from core.cpu_detect import get_cpu_profile
    from core.database import AsyncSessionLocal, FitRun, ModelRecord, engine
    from core.fit import build_report, summarize_model
    from core.model_orchestrator import container_memory_mb, container_name_for, ensure_model_running

    try:
        async with AsyncSessionLocal() as db:
            run = (await db.execute(select(FitRun).where(FitRun.id == run_id))).scalar_one_or_none()
            if not run:
                return
            run.status = "running"
            await db.commit()
            suite, model_ids, user_id = run.suite, run.model_ids, run.user_id
            models = (await db.execute(select(ModelRecord).where(ModelRecord.id.in_(model_ids)))).scalars().all()

        try:
            total = len(models) * len(suite["cases"])
            done = 0
            summaries = []
            async with httpx.AsyncClient(timeout=settings.INFERENCE_TIMEOUT_S) as client:
                for model in models:
                    endpoint = await ensure_model_running(model)
                    rows = []
                    for index, case in enumerate(suite["cases"]):
                        rows.append(await _run_case(client, endpoint, case, index))
                        done += 1
                        async with AsyncSessionLocal() as db:
                            live = (await db.execute(select(FitRun).where(FitRun.id == run_id))).scalar_one()
                            live.progress = {"done": done, "total": total, "model": model.name}
                            await db.commit()
                    memory = await container_memory_mb(container_name_for(model.id))
                    summaries.append(summarize_model(model.id, model.name, rows, memory))

            profile = get_cpu_profile().to_dict()
            report = build_report(
                suite, summaries,
                hardware={**profile, "threads_per_model": THREADS_PER_MODEL},
                pricing={
                    "cloud_label": settings.CLOUD_PRICE_LABEL,
                    "cloud_usd_per_m_input": settings.CLOUD_INPUT_USD_PER_M,
                    "cloud_usd_per_m_output": settings.CLOUD_OUTPUT_USD_PER_M,
                    "server_label": settings.SERVER_PRICE_LABEL,
                    "server_usd_per_hour": settings.SERVER_USD_PER_HOUR,
                },
            )
            status, error = "completed", None
        except Exception as e:
            report, status, error = None, "failed", str(e)[:500]

        async with AsyncSessionLocal() as db:
            live = (await db.execute(select(FitRun).where(FitRun.id == run_id))).scalar_one()
            live.status, live.report, live.error = status, report, error
            live.completed_at = datetime.now(timezone.utc)
            await db.commit()
        await audit.record_event(
            f"fit_run.{status}", actor_user_id=user_id, route="local", status=status,
            detail={"run_id": run_id, "models": model_ids, "cases": len(suite["cases"])},
        )
    finally:
        # asyncio.run gives each task a new event loop; pooled connections can't cross loops.
        await engine.dispose()
