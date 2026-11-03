"""
Dynamic multi-model serving orchestrator.

llama-server is single-model-per-process (infra/llamacpp/start.sh takes one
--model and one --port, no loop over multiple models). This module is what
makes serving more than one GGUF model at a time possible anyway: it starts a
short-lived llama.cpp container per model on demand, through the scoped Docker
proxy (infra/docker-proxy/proxy.py), and evicts the least-recently-used instance
once MAX_CONCURRENT_INSTANCES are resident.

ModelInstance rows are the source of truth for "is this model actually being
served right now" — model_records.status=READY only means the weights are on
disk and registered, not that a llama-server process has them loaded.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.config import settings
from core.database import AsyncSessionLocal, InstanceStatus, ModelInstance, ModelRecord

MAX_CONCURRENT_INSTANCES = 2
LLAMACPP_IMAGE = "meshpilot-llamacpp"
MODEL_STORAGE_VOLUME = "meshpilot_model-storage"
NETWORK_NAME = "meshpilot_default"
CONTAINER_PORT = 8080

# Separate from the fixed cpuset ranges already claimed by api/onnx/worker
# (0-3) and pytorch-baseline (4-7), so dynamic instances don't contend with
# either for the same cores.
DYNAMIC_CPUSET = "8-11"

# One slot gives a request the whole 4096-token context, instead of a quarter of
# it. Documents matter more here than parallelism: an instance only has 4 threads
# to share anyway, and requests beyond the first wait their turn.
PARALLEL_SLOTS = 1

# Tokens kept free for the chat template, which /tokenize doesn't see.
CONTEXT_RESERVE_TOKENS = 32

STARTUP_TIMEOUT_S = 180.0
POLL_INTERVAL_S = 1.0


class PromptTooLong(ValueError):
    pass


def _docker_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=settings.DOCKER_PROXY_URL, timeout=30.0)


def container_name_for(model_id: str) -> str:
    # model_id is either a UUID (user-uploaded models) or a seeded slug like
    # "benchmark-qwen25-05b" -- both are within Docker's [a-zA-Z0-9_.-] name charset.
    return f"meshpilot-dyn-{model_id}"[:63]


def _resource_limits(parameters_b: Optional[float]) -> tuple[str, int]:
    """(cpuset, mem_limit_bytes), sized off the model's parameter count —
    roughly 0.7GB per billion params at Q4 quantization, plus a flat 1GB for
    the KV cache and runtime overhead."""
    params = parameters_b or 1.0
    mem_gb = max(1.0, params * 0.7 + 1.0)
    return DYNAMIC_CPUSET, int(mem_gb * 1024 * 1024 * 1024)


async def container_memory_mb(container_name: str) -> Optional[float]:
    """A container's current real memory use, the same figure `docker stats`
    shows (usage minus reclaimable page cache). None if it can't be read."""
    try:
        async with _docker_client() as client:
            resp = await client.get(f"/containers/{container_name}/stats", params={"stream": "false"})
            resp.raise_for_status()
            mem = resp.json().get("memory_stats", {})
        usage = mem.get("usage")
        if usage is None:
            return None
        stats = mem.get("stats", {})
        cache = stats.get("inactive_file", stats.get("cache", 0))
        return round((usage - cache) / (1024 * 1024), 1)
    except Exception:
        return None


async def fit_to_context(endpoint: str, text: str, max_tokens: int) -> int:
    """Returns how many tokens may be generated, reduced if needed to fit.

    llama-server silently drops the middle of a prompt longer than its per-request
    context and answers as if it had read everything, so that's refused here. If
    the check itself can't run, the request goes ahead rather than failing."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            props = (await client.get(f"{endpoint}/props")).json()
            tokens = (await client.post(f"{endpoint}/tokenize", json={"content": text})).json()["tokens"]
        n_ctx = props["default_generation_settings"]["n_ctx"]
    except (httpx.HTTPError, ValueError, KeyError):
        return max_tokens
    available = n_ctx - len(tokens) - CONTEXT_RESERVE_TOKENS
    if available < 1:
        raise PromptTooLong(
            f"This prompt is about {len(tokens)} tokens, but this model can read at most "
            f"{n_ctx - CONTEXT_RESERVE_TOKENS - 1} per request. Shorten it or split it into parts."
        )
    return min(max_tokens, available)


async def _get_instance(db, model_id: str) -> Optional[ModelInstance]:
    result = await db.execute(select(ModelInstance).where(ModelInstance.model_id == model_id))
    return result.scalar_one_or_none()


async def _find_container(client: httpx.AsyncClient, name: str) -> Optional[dict]:
    resp = await client.get(
        "/containers/json",
        params={"all": "true", "filters": f'{{"name":["^/{name}$"]}}'},
    )
    resp.raise_for_status()
    containers = resp.json()
    return containers[0] if containers else None


async def _create_and_start_container(client: httpx.AsyncClient, model: ModelRecord, name: str) -> None:
    if not model.file_path:
        raise RuntimeError(f"Model {model.id} has no file_path set")
    model_filename = model.file_path.rsplit("/", 1)[-1]
    cpuset, mem_limit = _resource_limits(model.parameters_b)

    create_payload = {
        "Image": LLAMACPP_IMAGE,
        "Env": [
            "LLAMA_MODEL_DIR=/models",
            "LLAMA_HOST=0.0.0.0",
            "LLAMA_PORT=8080",
            "LLAMA_THREADS=4",
            "LLAMA_CTX_SIZE=4096",
            "LLAMA_BATCH_SIZE=512",
            f"LLAMA_PARALLEL={PARALLEL_SLOTS}",
            f"LLAMA_MODEL_FILE={model_filename}",
        ],
        "HostConfig": {
            "CpusetCpus": cpuset,
            "Memory": mem_limit,
            "Binds": [f"{MODEL_STORAGE_VOLUME}:/models:ro"],
            "NetworkMode": NETWORK_NAME,
            "RestartPolicy": {"Name": "no"},
        },
    }
    resp = await client.post("/containers/create", params={"name": name}, json=create_payload)
    if resp.status_code != 409:  # 409 = a container with this name already exists -- reuse it
        resp.raise_for_status()

    start_resp = await client.post(f"/containers/{name}/start")
    if start_resp.status_code not in (204, 304):  # 304 = already started
        start_resp.raise_for_status()


async def _stop_and_remove_container(client: httpx.AsyncClient, name: str) -> None:
    try:
        await client.post(f"/containers/{name}/stop", params={"t": "10"})
    except Exception:
        pass
    try:
        await client.delete(f"/containers/{name}", params={"force": "true", "v": "true"})
    except Exception:
        pass


async def _wait_healthy(endpoint_url: str, timeout_s: float = STARTUP_TIMEOUT_S) -> None:
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient(timeout=5.0) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(f"{endpoint_url}/health")
                if resp.status_code == 200:
                    return
            except Exception:
                pass
            await asyncio.sleep(POLL_INTERVAL_S)
    raise TimeoutError(f"{endpoint_url} did not become healthy within {timeout_s}s")


async def _evict_lru_if_at_cap(db, keep_model_id: str) -> None:
    result = await db.execute(
        select(ModelInstance)
        .where(ModelInstance.model_id != keep_model_id)
        .order_by(ModelInstance.last_used_at.asc())
    )
    instances = result.scalars().all()
    if len(instances) < MAX_CONCURRENT_INSTANCES:
        return

    victim = instances[0]
    victim_name = victim.container_name
    victim.status = InstanceStatus.STOPPING
    await db.commit()

    async with _docker_client() as client:
        await _stop_and_remove_container(client, victim_name)

    await db.delete(victim)
    await db.commit()


async def ensure_model_running(model: ModelRecord) -> str:
    """Returns a base URL (e.g. http://meshpilot-dyn-<id>:8080), confirmed
    healthy, serving `model`. Starts a fresh container on demand if none is
    resident yet — evicting the least-recently-used instance first if
    already at MAX_CONCURRENT_INSTANCES."""
    name = container_name_for(model.id)
    endpoint = f"http://{name}:{CONTAINER_PORT}"

    async with AsyncSessionLocal() as db:
        instance = await _get_instance(db, model.id)

        if instance is None:
            await _evict_lru_if_at_cap(db, keep_model_id=model.id)
            instance = ModelInstance(
                model_id=model.id,
                container_name=name,
                endpoint_url=endpoint,
                status=InstanceStatus.STARTING,
            )
            db.add(instance)
            try:
                await db.commit()
            except IntegrityError:
                # Another concurrent request already created this model's
                # instance row -- fall through to reuse/poll it instead of
                # racing to create a second container.
                await db.rollback()
                instance = await _get_instance(db, model.id)

    if instance is not None and instance.status == InstanceStatus.READY:
        # A READY row can outlive its container (host reboot, compose down), so
        # confirm the container is really running before handing out its URL.
        async with _docker_client() as client:
            existing = await _find_container(client, name)
        running = existing is not None and existing.get("State") == "running"
        async with AsyncSessionLocal() as db:
            live = await _get_instance(db, model.id)
            if live is not None:
                if running:
                    live.last_used_at = datetime.now(timezone.utc)
                else:
                    live.status = InstanceStatus.STARTING
                await db.commit()
        if running:
            return endpoint

    async with _docker_client() as client:
        existing = await _find_container(client, name)
        if existing is None:
            await _create_and_start_container(client, model, name)
        elif existing.get("State") != "running":
            # Recreate rather than restart: a stopped container can reference
            # a network that compose has since removed and recreated.
            await _stop_and_remove_container(client, name)
            await _create_and_start_container(client, model, name)

    await _wait_healthy(endpoint)

    async with AsyncSessionLocal() as db:
        live = await _get_instance(db, model.id)
        if live is not None:
            live.status = InstanceStatus.READY
            live.last_used_at = datetime.now(timezone.utc)
            await db.commit()

    return endpoint
