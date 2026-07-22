"""MeshPilot Celery application — async inference and quantization tasks."""

from celery import Celery
from core.config import settings

celery_app = Celery(
    "meshpilot",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    # BUGFIX: workers/webhook_worker.py does not exist in this repo — including
    # it made the celery worker die at startup with ModuleNotFoundError.
    include=["workers.inference_worker", "workers.quantize_worker"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_routes={
        "workers.inference_worker.*": {"queue": "inference"},
        "workers.quantize_worker.*":  {"queue": "quantize"},
    },
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # BUGFIX: these were 110/130 while INFERENCE_TIMEOUT_S is 120, so Celery
    # killed a slow inference 10s BEFORE the HTTP client would time out — and the
    # SoftTimeLimitExceeded surfaced as a generic failure. Derive from the
    # configured budget so the limits always sit above it.
    task_soft_time_limit=settings.INFERENCE_TIMEOUT_S + 20,
    task_time_limit=settings.INFERENCE_TIMEOUT_S + 40,
)
