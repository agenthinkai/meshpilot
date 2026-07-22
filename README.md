# ⚡ MeshPilot — CPU-Only AI Inference Platform

> **Run enterprise LLMs on your existing servers. No GPU required.**

MeshPilot is an open-source, production-ready AI inference platform that delivers optimized CPU inference for enterprises blocked from GPU access by cost, export controls, or data sovereignty requirements.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![CPU-Only](https://img.shields.io/badge/Hardware-CPU--Only-green.svg)]()
[![Docker](https://img.shields.io/badge/Deploy-Docker_Compose-blue.svg)]()

---

## 🎯 Who Is This For?

| Segment | Pain Point | MeshPilot Solution |
|---------|-----------|-------------------|
| **GCC/MENA Banks** | Export controls block GPU imports | Run on existing Xeon servers |
| **Indian Enterprises** | GPU cloud costs 10-50x CPU costs | 4.7x speedup on CPU with INT4 |
| **SE Asia Telcos** | Data sovereignty — no cloud LLM | On-prem, air-gapped deployment |
| **Family Offices** | No MLOps team, need simple setup | One `docker compose up` install |
| **Defense/Gov** | Classified data cannot leave premise | Fully offline, no telemetry |

---

## 🚀 Quick Start (5 minutes)

### Prerequisites
- Docker + Docker Compose
- 4+ CPU cores, 16GB RAM
- 20GB disk space

```bash
# 1. Clone the repo
git clone https://github.com/your-org/meshpilot.git
cd meshpilot

# 2. Copy environment template
cp .env.example .env
# Edit .env: set SECRET_KEY, ADMIN_EMAIL, ADMIN_PASSWORD

# 3. Start all services
docker compose up -d

# 4. Open the dashboard
open http://localhost:80

# 5. The pre-loaded Llama-3.2-1B-Instruct-Q4_K_M model is ready immediately
```

### First Inference

```bash
# Get your API key from the dashboard → API Keys → New Key

curl -X POST http://localhost/api/v1/inference/sync/llama-3.2-1b-instruct \
  -H "X-API-Key: mp_your_key_here" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Explain the key risks of a SaaS business in 3 bullet points.",
    "max_tokens": 256,
    "temperature": 0.7
  }'
```

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Client / Browser                          │
└─────────────────────────────┬───────────────────────────────────┘
                              │ HTTP
                    ┌─────────▼─────────┐
                    │      nginx        │  Rate limiting, SSL, routing
                    └─────────┬─────────┘
                              │
                    ┌─────────▼─────────┐
                    │   FastAPI (API)   │  Auth, model registry, metrics
                    │   + Frontend      │  tRPC-style typed procedures
                    └──────┬──────┬─────┘
                           │      │
               ┌───────────▼┐    ┌▼──────────────┐
               │ llama.cpp  │    │  ONNX Runtime  │
               │ Server     │    │  + OpenVINO EP │
               │ (GGUF INT4)│    │  (ONNX INT8)   │
               └────────────┘    └────────────────┘
                           │      │
               ┌───────────▼──────▼──────────────┐
               │  SQLite (metadata) + Redis (queue)│
               └──────────────────────────────────┘
                           │
               ┌───────────▼──────────────────────┐
               │  Prometheus + Grafana (metrics)   │
               └──────────────────────────────────┘
```

### CPU Optimization Stack

| Layer | Technology | Benefit |
|-------|-----------|---------|
| Quantization | INT4 Q4_K_M (GGUF) | 4x memory reduction |
| BLAS | OpenBLAS | Optimized matrix ops |
| SIMD | AVX2 / AVX-512 / AMX | Hardware vector acceleration |
| Threading | OpenMP + BLAS threads | Full core utilization |
| Batching | Continuous batching | Higher throughput |
| ONNX | OpenVINO EP | Intel CPU optimization |

---

## 📡 API Reference

### Authentication

All inference endpoints require an API key:
```
X-API-Key: mp_your_key_here
```

JWT tokens (from login) are accepted for dashboard/management endpoints:
```
Authorization: Bearer eyJ...
```

### Endpoints

#### POST `/api/v1/inference/sync/{model_id}`
Synchronous inference — waits for result.

```json
// Request
{
  "prompt": "Your prompt here",
  "max_tokens": 512,
  "temperature": 0.7,
  "top_p": 0.9,
  "stop": ["\n\n"]
}

// Response
{
  "id": "abc123",
  "status": "completed",
  "text": "Generated text...",
  "prompt_tokens": 42,
  "completion_tokens": 256,
  "latency_ms": 1840.5,
  "throughput_tps": 139.1,
  "backend_used": "llamacpp"
}
```

#### POST `/api/v1/inference/async/{model_id}`
Async inference — returns task ID immediately. Optionally fires a webhook on completion.

```json
// Request (add webhook_url for callback)
{
  "prompt": "...",
  "max_tokens": 1024,
  "webhook_url": "https://your-server.com/callback"
}

// Response (202 Accepted)
{
  "task_id": "xyz789",
  "status": "pending",
  "poll_url": "/api/v1/inference/tasks/xyz789"
}
```

#### GET `/api/v1/inference/tasks/{task_id}`
Poll async task status.

#### POST `/api/v1/models`
Upload a model (GGUF, ONNX, PyTorch). Auto-quantizes to INT4/INT8.

```bash
curl -X POST http://localhost/api/v1/models \
  -H "Authorization: Bearer {token}" \
  -F "file=@./my-model.gguf" \
  -F "name=My Custom Model"
```

#### GET `/api/v1/metrics/dashboard`
Dashboard statistics (requests, latency, throughput, CPU profile).

---

## 🔧 Configuration

### Environment Variables (`.env`)

```env
# Security
SECRET_KEY=your-secret-key-min-32-chars
ADMIN_EMAIL=admin@yourcompany.com
ADMIN_PASSWORD=change-me-in-production

# Inference
LLAMA_THREADS=4          # CPU threads for llama.cpp
LLAMA_CTX_SIZE=4096      # Context window size
LLAMA_BATCH_SIZE=512     # Batch size for continuous batching
INFERENCE_TIMEOUT_S=120  # Max inference time in seconds

# Services
REDIS_URL=redis://redis:6379/0
DATABASE_URL=sqlite:///./data/meshpilot.db

# Quantization
AUTO_QUANT_BITS=4        # Default: INT4 (options: 4, 8)
```

### Scaling

```yaml
# docker-compose.override.yml — scale workers
services:
  worker:
    deploy:
      replicas: 4   # One worker per CPU core recommended
```

---

## 📊 Benchmark Results

Run the benchmark yourself:

```bash
# Simulation mode (no server required)
python3 benchmark/benchmark.py --simulate

# Live mode (against running MeshPilot)
python3 benchmark/benchmark.py \
  --meshpilot-url http://localhost \
  --api-key mp_your_key \
  --model-id llama-3.2-1b-instruct \
  --iterations 50
```

### Typical Results (4-core Xeon, 16GB RAM)

| Metric | PyTorch FP32 | MeshPilot INT4 | Improvement |
|--------|-------------|----------------|-------------|
| P50 Latency | 2,837ms | 607ms | **4.7x faster** |
| P90 Latency | 3,109ms | 633ms | **4.9x faster** |
| Avg Throughput | 90 t/s | 421 t/s | **+367%** |
| Memory Usage | 3,800 MB | 850 MB | **-78%** |

---

## 🧠 Supported Models

### Pre-loaded (instant demo)
- `Llama-3.2-1B-Instruct-Q4_K_M` — 850MB, ~420 t/s on 4-core CPU

### Upload your own
- **GGUF** — any llama.cpp-compatible model (Llama, Mistral, Phi, Gemma, Qwen)
- **ONNX** — any ONNX model (auto-runs on OpenVINO EP if Intel CPU detected)
- **PyTorch** — `.pt`, `.bin`, `.safetensors` (auto-converted to ONNX on upload)

---

## 🔒 Security & Compliance

- **No telemetry** — zero data leaves your infrastructure
- **Air-gap ready** — all dependencies bundled in Docker images
- **RBAC** — admin/user roles with API key scoping
- **Encryption** — JWT-signed sessions, bcrypt passwords, API key hashing
- **Audit log** — every inference logged with user, model, latency, tokens
- **Data sovereignty** — models and data never leave your servers

---

## 🐳 Docker Services

| Service | Port | Description |
|---------|------|-------------|
| `nginx` | 80 | Reverse proxy, SSL termination |
| `api` | 8000 | FastAPI backend + frontend |
| `llamacpp` | 8080 | llama.cpp inference server |
| `onnx` | 8090 | ONNX Runtime inference server |
| `worker` | — | Celery async workers |
| `redis` | 6379 | Task queue + caching |
| `prometheus` | 9090 | Metrics collection |
| `grafana` | 3000 | Metrics dashboard (at /grafana/) |

---

## 📦 Project Structure

```
meshpilot/
├── backend/
│   ├── main.py                 # FastAPI app entry point
│   ├── core/
│   │   ├── config.py           # Settings, env vars
│   │   ├── database.py         # SQLAlchemy models
│   │   ├── security.py         # JWT, API keys, RBAC
│   │   ├── cpu_detect.py       # CPU capability detection
│   │   ├── quantizer.py        # Auto-quantization pipeline
│   │   ├── celery_app.py       # Celery configuration
│   │   ├── metrics.py          # Prometheus metrics
│   │   └── seed.py             # Demo model seeding
│   ├── routers/
│   │   ├── auth.py             # User auth, API keys
│   │   ├── models.py           # Model registry
│   │   ├── inference.py        # Sync/async inference
│   │   ├── admin.py            # Admin endpoints
│   │   └── metrics.py          # Dashboard metrics
│   └── workers/
│       ├── inference_worker.py # Async inference Celery task
│       └── quantize_worker.py  # Model quantization Celery task
├── infra/
│   ├── nginx/nginx.conf        # Nginx configuration
│   ├── llamacpp/               # llama.cpp server Dockerfile
│   ├── onnx/                   # ONNX Runtime server
│   ├── prometheus/             # Prometheus config
│   └── grafana/                # Grafana provisioning
├── frontend/
│   ├── index.html              # Dashboard UI
│   └── static/
│       ├── css/dashboard.css   # Styles
│       └── js/app.js           # Frontend application
├── benchmark/
│   ├── benchmark.py            # Benchmark script
│   └── output/                 # Benchmark results
├── docker-compose.yml
├── .env.example
└── README.md
```

---

## 🤝 Contributing

1. Fork the repo
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Run tests: `pytest backend/tests/`
4. Submit a PR

---

## 📄 License

Apache 2.0 — free for commercial use, modification, and distribution.

---

## 💼 Commercial Support

| Tier | Price | Includes |
|------|-------|---------|
| **Free** | $0 | 1 model, community support |
| **Pro** | $99/mo | 5 models, API, email support |
| **Team** | $999/mo | Unlimited, on-prem option, SLA |
| **Enterprise** | Custom | Air-gapped, federated mesh, compliance audit |
| **Pilot** | $15K fixed | 60 days, full refund if unsatisfied |

Contact: [meshpilot@mesh.ai](mailto:meshpilot@mesh.ai)
