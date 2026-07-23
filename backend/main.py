"""
MeshPilot — CPU-Only AI Inference Platform
FastAPI Application Entry Point
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from core.database import init_db
from core.config import settings
from core.metrics import REGISTRY
from routers import auth, models, inference, admin, metrics as metrics_router

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    logger.info("meshpilot.startup", version="1.0.0-mvp", cpu_only=True)
    await init_db()
    # Seed demo model record and bootstrap admin account if not present
    from core.seed import seed_demo_models, seed_admin_user
    await seed_demo_models()
    await seed_admin_user()
    yield
    logger.info("meshpilot.shutdown")


app = FastAPI(
    title="MeshPilot API",
    description=(
        "CPU-Only AI Inference Platform for enterprises blocked from GPU access "
        "by cost, export controls, or data sovereignty requirements."
    ),
    version="1.0.0-mvp",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

# ── Middleware ────────────────────────────────────────────────────────────────

app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    import uuid
    request_id = str(uuid.uuid4())[:8]
    request.state.request_id = request_id
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start) * 1000
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Response-Time"] = f"{duration_ms:.1f}ms"
    return response


# ── Prometheus instrumentation ────────────────────────────────────────────────

Instrumentator(
    should_group_status_codes=True,
    should_ignore_untemplated=True,
    excluded_handlers=["/health", "/metrics"],
).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(auth.router,       prefix="/api/v1/auth",      tags=["Authentication"])
app.include_router(models.router,     prefix="/api/v1/models",    tags=["Model Registry"])
app.include_router(inference.router,  prefix="/api/v1/inference", tags=["Inference"])
app.include_router(admin.router,      prefix="/api/v1/admin",     tags=["Admin"])
app.include_router(metrics_router.router, prefix="/api/v1/metrics", tags=["Metrics"])

# ── Health & root ─────────────────────────────────────────────────────────────

@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok", "service": "meshpilot-api", "version": "1.0.0-mvp"}


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error("unhandled_exception", path=request.url.path, error=str(exc))
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "request_id": getattr(request.state, "request_id", "")},
    )


# ── Static frontend ───────────────────────────────────────────────────────────
# BUGFIX: this mount MUST stay last. Starlette matches routes in registration
# order and Mount("/") matches every path, so when this sat above /health it
# swallowed it (404) — breaking the compose healthcheck and nginx's /health
# location whenever /app/frontend exists.

if os.path.exists("/app/frontend"):
    app.mount("/", StaticFiles(directory="/app/frontend", html=True), name="frontend")
