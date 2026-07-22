"""MeshPilot Prometheus metrics registry."""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, Summary

REGISTRY = CollectorRegistry(auto_describe=True)

# ── Inference metrics ─────────────────────────────────────────────────────────

INFERENCE_REQUESTS_TOTAL = Counter(
    "meshpilot_inference_requests_total",
    "Total inference requests",
    ["model_id", "backend", "status"],
    registry=REGISTRY,
)

INFERENCE_LATENCY_MS = Histogram(
    "meshpilot_inference_latency_ms",
    "End-to-end inference latency in milliseconds",
    ["model_id", "backend"],
    buckets=[50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000],
    registry=REGISTRY,
)

INFERENCE_TTFT_MS = Histogram(
    "meshpilot_inference_ttft_ms",
    "Time-to-first-token in milliseconds",
    ["model_id", "backend"],
    buckets=[25, 50, 100, 250, 500, 1000, 2500, 5000],
    registry=REGISTRY,
)

TOKENS_GENERATED_TOTAL = Counter(
    "meshpilot_tokens_generated_total",
    "Total tokens generated",
    ["model_id", "backend"],
    registry=REGISTRY,
)

THROUGHPUT_TPS = Gauge(
    "meshpilot_throughput_tokens_per_second",
    "Current tokens per second",
    ["model_id", "backend"],
    registry=REGISTRY,
)

# ── Model metrics ─────────────────────────────────────────────────────────────

MODELS_LOADED = Gauge(
    "meshpilot_models_loaded_total",
    "Number of models currently loaded",
    registry=REGISTRY,
)

MODEL_UPLOAD_TOTAL = Counter(
    "meshpilot_model_uploads_total",
    "Total model uploads",
    ["format", "status"],
    registry=REGISTRY,
)

QUANTIZATION_DURATION_S = Histogram(
    "meshpilot_quantization_duration_seconds",
    "Time taken to quantize a model",
    ["format", "quant_bits"],
    buckets=[10, 30, 60, 120, 300, 600, 1200],
    registry=REGISTRY,
)

# ── Queue metrics ─────────────────────────────────────────────────────────────

QUEUE_DEPTH = Gauge(
    "meshpilot_queue_depth",
    "Number of pending inference tasks in queue",
    ["queue_name"],
    registry=REGISTRY,
)

# ── System metrics ────────────────────────────────────────────────────────────

CPU_USAGE_PCT = Gauge(
    "meshpilot_cpu_usage_percent",
    "CPU usage percentage",
    registry=REGISTRY,
)

MEMORY_USAGE_PCT = Gauge(
    "meshpilot_memory_usage_percent",
    "Memory usage percentage",
    registry=REGISTRY,
)
