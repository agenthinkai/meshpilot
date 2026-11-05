"""MeshPilot configuration — loaded from environment variables."""

from __future__ import annotations
from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Core
    SECRET_KEY: str = "changeme-in-production-32chars!!"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 24 hours
    API_KEY_PREFIX: str = "mp_"
    # "production" refuses to start with placeholder secrets (core/startup_checks.py).
    MESHPILOT_ENV: str = "production"
    AUDIT_HMAC_KEY: str = ""
    LOGIN_MAX_FAILURES: int = 5
    LOGIN_WINDOW_S: int = 900
    # Pickle-based formats can run code when loaded, so uploads refuse them by default.
    ALLOW_PICKLE_UPLOADS: bool = False

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://meshpilot:changeme-in-production@postgres:5432/meshpilot"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # Storage
    MODEL_STORAGE: str = "/models"
    MAX_MODEL_SIZE_GB: float = 8.0

    # Inference backends
    # No LLAMACPP_URL -- llama-server is single-model-per-process, so there is
    # no one standing endpoint; core/model_orchestrator.py starts a dynamic
    # per-model container on demand and returns its endpoint URL directly.
    ONNX_URL: str = "http://onnx:8090"
    PYTORCH_BASELINE_URL: str = "http://pytorch-baseline:8091"
    INFERENCE_TIMEOUT_S: int = 120
    QUEUE_MAX_WAIT_S: int = 600
    EMBEDDER_URL: str = "http://embedder:8095"
    DOCUMENTS_DEFAULT_MODEL: str = "allam-7b"
    FIT_TIMEOUT_S: int = 7200
    # Only this proxy touches the Docker socket; see infra/docker-proxy/proxy.py.
    DOCKER_PROXY_URL: str = "http://docker-proxy:2375"
    # Used when a request asks for `auto` or `cloud` but has to run locally.
    LOCAL_DEFAULT_MODEL: str = "benchmark-mistral-7b"

    # Cloud routing. Off unless this server switch AND the dashboard policy are
    # both on, so a dashboard admin alone can't send data off the server.
    CLOUD_ROUTING_ENABLED: bool = False
    CLOUD_API_BASE: str = ""   # any OpenAI-compatible API, e.g. https://api.openai.com/v1
    CLOUD_API_KEY: str = ""
    CLOUD_MODEL: str = ""
    # Some providers' newer models require max_completion_tokens instead.
    CLOUD_MAX_TOKENS_PARAM: str = "max_tokens"

    # CORS
    # The dashboard is same-origin, so other sites get no access unless listed.
    CORS_ORIGINS: List[str] = []

    # Quantization
    DEFAULT_QUANT_BITS: int = 8  # INT8 default, INT4 for GGUF
    QUANT_CALIBRATION_SAMPLES: int = 128

    # Rate limiting (requests per minute per API key)
    FREE_TIER_RPM: int = 10
    PRO_TIER_RPM: int = 200
    TEAM_TIER_RPM: int = 2000

    # Logging
    LOG_LEVEL: str = "info"

    # Demo model
    DEMO_MODEL_HF_REPO: str = "bartowski/Llama-3.2-1B-Instruct-GGUF"
    DEMO_MODEL_FILENAME: str = "Llama-3.2-1B-Instruct-Q4_K_M.gguf"

    # Savings panel pricing. These are list prices used for labelled estimates,
    # not measurements; override per customer through the environment.
    CLOUD_PRICE_LABEL: str = "Gemini 2.5 Flash-Lite"
    CLOUD_INPUT_USD_PER_M: float = 0.10
    CLOUD_OUTPUT_USD_PER_M: float = 0.40
    SERVER_PRICE_LABEL: str = "AWS m7i.xlarge on-demand, 4 vCPU"
    SERVER_USD_PER_HOUR: float = 0.2016

    # Admin bootstrap (optional — creates an admin user on first startup if set)
    ADMIN_EMAIL: str = ""
    ADMIN_PASSWORD: str = ""


settings = Settings()
