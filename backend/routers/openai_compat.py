"""
OpenAI-compatible API, so existing apps can switch by changing their base URL.

Endpoints:
  GET  /v1/models
  GET  /v1/models/{model}
  POST /v1/chat/completions   — streaming and non-streaming, routed by the privacy gateway
  POST /v1/completions        — local models only

`model` is a local model's id or slug, `auto`, or `cloud`. See core/privacy.py
for how a request is routed. The X-MeshPilot-Route response header says where
it ran.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import aclosing
from datetime import datetime, timezone
from typing import AsyncIterator, Callable, List, Optional, Union

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core import audit
from core.config import settings
from core.database import (
    AsyncSessionLocal, InferenceLog, InferenceStatus, ModelRecord, ModelStatus, PrivacyPolicy, User, get_db,
)
from core.metrics import (
    INFERENCE_LATENCY_MS, INFERENCE_REQUESTS_TOTAL, THROUGHPUT_TPS, TOKENS_GENERATED_TOTAL,
)
from core.model_orchestrator import PromptTooLong, ensure_model_running, fit_to_context
from core.privacy import BLOCKED, CLOUD, MODEL_AUTO, MODEL_CLOUD, decide_route, messages_text, scan_text
from core.ratelimit import enforce_user_rpm
from core.request_queue import Ticket
from core.security import get_openai_user

router = APIRouter()

UNSUPPORTED_PARAMS = ("tools", "tool_choice", "functions", "function_call", "logprobs", "top_logprobs")


class _OpenAIRequest(BaseModel):
    # Unknown fields are accepted, as OpenAI clients send many optional ones.
    # Fields that would change the answer's meaning are refused in _reject_unsupported.
    model_config = ConfigDict(extra="allow", protected_namespaces=())

    model: str
    max_tokens: Optional[int] = Field(default=None, ge=1, le=4096)
    max_completion_tokens: Optional[int] = Field(default=None, ge=1, le=4096)
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    top_p: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    stop: Optional[Union[str, List[str]]] = None
    stream: bool = False
    stream_options: Optional[dict] = None
    seed: Optional[int] = None
    n: int = 1
    response_format: Optional[dict] = None

    @property
    def token_limit(self) -> int:
        return self.max_completion_tokens or self.max_tokens or 512

    @property
    def wants_stream_usage(self) -> bool:
        return bool(self.stream_options and self.stream_options.get("include_usage"))


class ChatCompletionRequest(_OpenAIRequest):
    messages: List[dict] = Field(min_length=1)


class CompletionRequest(_OpenAIRequest):
    prompt: Union[str, List[str]]


def _reject_unsupported(req: _OpenAIRequest) -> None:
    extras = req.model_extra or {}
    for name in UNSUPPORTED_PARAMS:
        if extras.get(name):
            raise HTTPException(400, f"'{name}' is not supported by this server yet")
    if req.n != 1:
        raise HTTPException(400, "Only n=1 is supported")


def _sampling_params(req: _OpenAIRequest) -> dict:
    body = {"max_tokens": req.token_limit}
    for name in ("temperature", "top_p", "stop", "seed", "response_format"):
        value = getattr(req, name)
        if value is not None:
            body[name] = value
    return body


async def _resolve_local_model(name: str, user: User, db: AsyncSession) -> ModelRecord:
    result = await db.execute(select(ModelRecord).where(or_(ModelRecord.id == name, ModelRecord.slug == name)))
    model = result.scalars().first()
    if not model or (not model.is_public and model.owner_id != user.id):
        raise HTTPException(404, f"The model '{name}' does not exist")
    if model.status != ModelStatus.READY:
        raise HTTPException(409, f"Model '{name}' is not ready (status: {model.status.value})")
    if "onnx" in (model.backend or "llamacpp"):
        raise HTTPException(400, f"Model '{name}' uses the ONNX backend, which /v1 doesn't support yet")
    return model


async def _record_inference(user: User, model: ModelRecord, result: dict) -> None:
    status = InferenceStatus.COMPLETED if result["status"] == "completed" else InferenceStatus.FAILED
    async with AsyncSessionLocal() as db:
        db.add(InferenceLog(
            id=str(uuid.uuid4()), user_id=user.id, model_id=model.id, status=status,
            prompt_tokens=result.get("prompt_tokens"), completion_tokens=result.get("completion_tokens"),
            latency_ms=result.get("latency_ms"), ttft_ms=result.get("ttft_ms"),
            throughput_tps=result.get("throughput_tps"), backend_used="llamacpp",
            error_message=result.get("error"), completed_at=datetime.now(timezone.utc),
        ))
        await db.commit()
    INFERENCE_REQUESTS_TOTAL.labels(model_id=model.id, backend="llamacpp",
                                    status="success" if status == InferenceStatus.COMPLETED else "error").inc()
    if status == InferenceStatus.COMPLETED:
        INFERENCE_LATENCY_MS.labels(model_id=model.id, backend="llamacpp").observe(result["latency_ms"])
        TOKENS_GENERATED_TOTAL.labels(model_id=model.id, backend="llamacpp").inc(result.get("completion_tokens") or 0)
        THROUGHPUT_TPS.labels(model_id=model.id, backend="llamacpp").set(result.get("throughput_tps") or 0)


# Stays under nginx's 180s read timeout, because nothing can be sent while waiting.
NON_STREAM_MAX_WAIT_S = 150
QUEUE_POLL_S = 0.5
QUEUE_KEEPALIVE_S = 10


class _Upstream:
    """One call to a llama-server or to the cloud provider. Calls to a local
    model pass queue_model, and wait there for their turn."""

    def __init__(self, url: str, body: dict, headers: Optional[dict], on_finish: Callable[[dict], "asyncio.Future"],
                 model_label: str, transform: Optional[Callable[[dict], dict]] = None,
                 queue_model: Optional[str] = None):
        self.url, self.body, self.headers, self.on_finish = url, body, headers or {}, on_finish
        self.model_label, self.transform = model_label, transform
        self.started = time.perf_counter()
        self.ticket = Ticket(queue_model) if queue_model else None
        self.finished = False

    async def _finish(self, result: dict) -> None:
        if self.finished:
            return
        self.finished = True
        if self.ticket:
            await self.ticket.leave()
        await self.on_finish(result)

    async def _join_queue(self) -> int:
        if not self.ticket:
            return 0
        await self.ticket.join()
        return await self.ticket.requests_ahead()

    async def _start_turn(self) -> None:
        self.started = time.perf_counter()  # so speed figures leave out time spent waiting
        if self.ticket:
            await self.ticket.start_turn()

    async def stream(self, client_wants_usage: bool) -> StreamingResponse:
        ahead = await self._join_queue()
        if ahead:
            return StreamingResponse(self._wait_then_relay(ahead, client_wants_usage), **STREAM_RESPONSE)
        await self._start_turn()
        return await self.open_stream(client_wants_usage)

    async def answer(self, request: Request) -> dict:
        try:
            ahead = await self._join_queue()
            deadline = time.monotonic() + min(settings.QUEUE_MAX_WAIT_S, NON_STREAM_MAX_WAIT_S)
            while ahead:
                if await request.is_disconnected():
                    await self._finish(self._result("failed", error="left the queue before its turn"))
                    raise HTTPException(499, "The client disconnected")
                if time.monotonic() > deadline:
                    await self._finish(self._result("failed", error="gave up waiting in the queue"))
                    raise HTTPException(503, "This model is busy. Try again shortly.", headers={"Retry-After": "30"})
                await asyncio.sleep(QUEUE_POLL_S)
                ahead = await self.ticket.requests_ahead()
            await self._start_turn()
            return await self.complete()
        finally:
            if self.ticket:
                await self.ticket.leave()

    async def _wait_then_relay(self, ahead: int, client_wants_usage: bool) -> AsyncIterator[str]:
        """Places in line go out as SSE comments, which OpenAI clients ignore. They
        also keep the connection open, however long the line is."""
        deadline = time.monotonic() + settings.QUEUE_MAX_WAIT_S
        told, told_at = None, 0.0
        relaying = False
        try:
            while ahead:
                if ahead != told or time.monotonic() - told_at >= QUEUE_KEEPALIVE_S:
                    told, told_at = ahead, time.monotonic()
                    yield f": queue ahead={ahead}\n\n"
                if time.monotonic() > deadline:
                    await self._finish(self._result("failed", error="gave up waiting in the queue"))
                    yield _stream_error("This model is busy. Try again shortly.")
                    return
                await asyncio.sleep(QUEUE_POLL_S)
                ahead = await self.ticket.requests_ahead()
            yield ": queue ahead=0\n\n"
            await self._start_turn()
            try:
                client, resp = await self._connect()
            except HTTPException as e:
                yield _stream_error(e.detail)
                return
            relaying = True
            async with aclosing(self._relay(client, resp, client_wants_usage)) as chunks:
                async for chunk in chunks:
                    yield chunk
        finally:
            if not relaying:
                _run_in_background(self._finish(self._result("failed", error="left the queue before its turn")))

    def _result(self, status: str, usage: Optional[dict] = None, error: Optional[str] = None,
                ttft_ms: Optional[float] = None) -> dict:
        latency_ms = (time.perf_counter() - self.started) * 1000
        usage = usage or {}
        completion = usage.get("completion_tokens")
        return {
            "status": status, "error": error, "ttft_ms": ttft_ms,
            "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": completion,
            "latency_ms": round(latency_ms, 1),
            "throughput_tps": round(completion / (latency_ms / 1000), 2) if completion and latency_ms else None,
        }

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(settings.INFERENCE_TIMEOUT_S, connect=10.0)

    async def _fail(self, error: Exception, status_code: int, message: str) -> HTTPException:
        await self._finish(self._result("failed", error=str(error)[:500]))
        return HTTPException(status_code, message)

    async def complete(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=self._timeout()) as client:
                resp = await client.post(self.url, json=self.body, headers=self.headers)
            if resp.status_code >= 400:
                raise await self._upstream_error(resp.status_code, resp.text)
            data = resp.json()
            if self.transform:
                data = self.transform(data)
        except httpx.HTTPError as e:
            raise await self._fail(e, 502, "The model server did not respond")
        await self._finish(self._result("completed", data.get("usage")))
        return data

    async def _upstream_error(self, status_code: int, raw: str) -> HTTPException:
        # Client errors (too long a prompt, say) are the caller's to fix. Anything
        # else stays in the log, since provider errors can echo keys or internals.
        await self._finish(self._result("failed", error=raw[:500]))
        if status_code < 500 and status_code not in (401, 403, 429):
            return HTTPException(400, f"The model server rejected the request: {_error_message(raw)}")
        return HTTPException(502, "The model server returned an error")

    async def open_stream(self, client_wants_usage: bool) -> StreamingResponse:
        """Connects before responding, so upstream errors get a real status code."""
        client, resp = await self._connect()
        return StreamingResponse(self._relay(client, resp, client_wants_usage), **STREAM_RESPONSE)

    async def _connect(self) -> tuple[httpx.AsyncClient, httpx.Response]:
        client = httpx.AsyncClient(timeout=self._timeout())
        try:
            request = client.build_request("POST", self.url, json=self.body, headers=self.headers)
            resp = await client.send(request, stream=True)
        except httpx.HTTPError as e:
            await client.aclose()
            raise await self._fail(e, 502, "The model server did not respond")
        if resp.status_code >= 400:
            raw = (await resp.aread()).decode(errors="replace")
            await resp.aclose()
            await client.aclose()
            raise await self._upstream_error(resp.status_code, raw)
        return client, resp

    async def _relay(self, client: httpx.AsyncClient, resp: httpx.Response, client_wants_usage: bool) -> AsyncIterator[str]:
        usage, ttft_ms, status, error, content_chunks = None, None, "failed", "stream ended early", 0
        finished, last_chunk = False, {}
        try:
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    finished = True
                    continue
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue
                if self.transform:
                    chunk = self.transform(chunk)
                usage = chunk.pop("usage", None) or usage
                if self.ticket:
                    await self.ticket.still_answering()
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                if ttft_ms is None:
                    ttft_ms = round((time.perf_counter() - self.started) * 1000, 1)
                content_chunks += sum(1 for c in choices if (c.get("delta") or {}).get("content") or c.get("text"))
                finished = finished or any(c.get("finish_reason") for c in choices)
                chunk["model"] = self.model_label
                last_chunk = chunk
                yield f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n"

            if finished:
                status, error = "completed", None
                # Sent the way OpenAI does, even though llama-server attaches usage
                # to its last choice and sends no [DONE].
                if client_wants_usage and usage:
                    summary = {k: v for k, v in last_chunk.items() if k != "choices"}
                    yield f"data: {json.dumps({**summary, 'choices': [], 'usage': usage}, separators=(',', ':'))}\n\n"
                yield "data: [DONE]\n\n"
            else:
                yield f"data: {json.dumps({'error': {'message': 'The model server stopped early', 'type': 'server_error'}})}\n\n"
        except httpx.HTTPError as e:
            error = str(e)[:500]
            yield f"data: {json.dumps({'error': {'message': 'The model server stopped responding', 'type': 'server_error'}})}\n\n"
        finally:
            await resp.aclose()
            await client.aclose()
            if usage is None and content_chunks:
                usage = {"completion_tokens": content_chunks}
            _run_in_background(self._finish(self._result(status, usage, error, ttft_ms)))


_background_tasks: set = set()
STREAM_RESPONSE = {"media_type": "text/event-stream", "headers": {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}}


def _run_in_background(coroutine) -> None:
    # As its own task, because a client disconnect cancels the generator calling this.
    task = asyncio.ensure_future(coroutine)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _stream_error(message: str) -> str:
    return f"data: {json.dumps({'error': {'message': message, 'type': 'server_error'}})}\n\n"


def _error_message(raw: str) -> str:
    try:
        error = json.loads(raw).get("error")
        return str(error.get("message") if isinstance(error, dict) else error)[:300]
    except (ValueError, AttributeError):
        return "invalid request"


async def _limit_to_context(endpoint: str, text: str, max_tokens: int, on_finish) -> int:
    try:
        return await fit_to_context(endpoint, text, max_tokens)
    except PromptTooLong as e:
        await on_finish({"status": "failed", "error": str(e)[:500]})
        raise HTTPException(400, str(e))


def _native_to_openai_completion(model_id: str, stream: bool) -> Callable[[dict], dict]:
    """This llama-server build ignores max_tokens on /v1/completions and answers
    in its own format, so text completions use /completion and are converted."""
    completion_id = f"cmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    def convert(native: dict) -> dict:
        finish_reason = None
        if native.get("stop") or not stream:
            finish_reason = "length" if native.get("stopped_limit") else "stop"
        result = {
            "id": completion_id, "object": "text_completion", "created": created, "model": model_id,
            "choices": [{"index": 0, "text": native.get("content", ""), "logprobs": None, "finish_reason": finish_reason}],
        }
        if "tokens_predicted" in native:
            prompt_tokens, completion_tokens = native.get("tokens_evaluated", 0), native["tokens_predicted"]
            result["usage"] = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                               "total_tokens": prompt_tokens + completion_tokens}
        return result
    return convert


def _route_headers(route: str, reason: str, model: str) -> dict:
    return {"X-MeshPilot-Route": route, "X-MeshPilot-Reason": reason, "X-MeshPilot-Model": model}


def _audit_context(request: Request, user: User, prompt: str) -> dict:
    return {
        "actor_user_id": user.id, "actor_email": user.email,
        "actor_key_prefix": getattr(request.state, "api_key_prefix", None),
        "request_id": getattr(request.state, "request_id", None),
        "prompt_fingerprint": audit.prompt_fingerprint(prompt),
    }


def _on_finish(context: dict, model_id: str, route: str, destination: str,
               user: User, model: Optional[ModelRecord]):
    async def finish(result: dict) -> None:
        if model is not None:
            await _record_inference(user, model, result)
        await audit.record_event(
            f"inference.{result['status']}", **context, model_id=model_id, route=route, destination=destination,
            prompt_tokens=result.get("prompt_tokens"), completion_tokens=result.get("completion_tokens"),
            status=result["status"], reason=(result.get("error") or "")[:200] or None,
        )
    return finish


# ── Models ────────────────────────────────────────────────────────────────────

def _model_entry(model_id: str, owner: str, created: Optional[datetime] = None) -> dict:
    return {"id": model_id, "object": "model", "created": int((created or datetime.now(timezone.utc)).timestamp()),
            "owned_by": owner}


async def _load_policy(db: AsyncSession) -> PrivacyPolicy:
    return (await db.execute(select(PrivacyPolicy).where(PrivacyPolicy.id == 1))).scalar_one()


def _cloud_available() -> bool:
    return bool(settings.CLOUD_ROUTING_ENABLED and settings.CLOUD_API_BASE
                and settings.CLOUD_API_KEY and settings.CLOUD_MODEL)


async def _visible_entries(user: User, db: AsyncSession) -> List[dict]:
    result = await db.execute(select(ModelRecord).where(
        ModelRecord.status == ModelStatus.READY,
        or_(ModelRecord.is_public == True, ModelRecord.owner_id == user.id),
    ))
    entries = [_model_entry(m.id, "meshpilot-local", m.created_at) for m in result.scalars().all()
               if "onnx" not in (m.backend or "llamacpp")]
    entries.append(_model_entry(MODEL_AUTO, "meshpilot"))
    if _cloud_available() and (await _load_policy(db)).cloud_enabled:
        entries.append(_model_entry(MODEL_CLOUD, "meshpilot-gateway"))
    return entries


@router.get("/models")
async def list_models(db: AsyncSession = Depends(get_db), user: User = Depends(get_openai_user)):
    return {"object": "list", "data": await _visible_entries(user, db)}


@router.get("/models/{model_id:path}")
async def get_model(model_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_openai_user)):
    for entry in await _visible_entries(user, db):
        if entry["id"] == model_id:
            return entry
    model = await _resolve_local_model(model_id, user, db)
    return _model_entry(model.id, "meshpilot-local", model.created_at)


# ── Chat completions ──────────────────────────────────────────────────────────

@router.post("/chat/completions")
async def chat_completions(
    req:     ChatCompletionRequest,
    request: Request,
    db:      AsyncSession = Depends(get_db),
    user:    User         = Depends(get_openai_user),
):
    _reject_unsupported(req)
    await enforce_user_rpm(user)

    policy = await _load_policy(db)
    text, fully_scannable = messages_text(req.messages)
    decision = decide_route(
        requested_model=req.model, text=text, fully_scannable=fully_scannable,
        cloud_available=_cloud_available(), cloud_enabled_by_policy=policy.cloud_enabled,
        on_sensitive=policy.on_sensitive, blocked_terms=policy.blocked_terms or [],
        auto_cloud_min_chars=policy.auto_cloud_min_chars,
    )
    context = _audit_context(request, user, text)

    if decision.route == BLOCKED:
        await db.commit()
        await audit.record_event(
            "inference.blocked", **context, route=BLOCKED, reason=decision.reason,
            sensitive=decision.sensitive, status="blocked",
        )
        raise HTTPException(403, f"Request not sent to the cloud: {decision.reason}")

    if decision.route == CLOUD:
        model = None
        model_id, destination = MODEL_CLOUD, httpx.URL(settings.CLOUD_API_BASE).host
    else:
        wants_default = req.model in (MODEL_AUTO, MODEL_CLOUD)
        model = await _resolve_local_model(settings.LOCAL_DEFAULT_MODEL if wants_default else req.model, user, db)
        model_id, destination = model.id, "local"

    # Gives the connection back rather than holding it through the wait and the answer.
    # With many requests waiting, those still to be recorded would run out of connections.
    await db.commit()

    # Recorded before any data moves. If this write fails the request fails too.
    await audit.record_event(
        "inference.routed", **context, model_id=model_id, route=decision.route, destination=destination,
        reason=decision.reason, sensitive=decision.sensitive, status="routed",
    )
    on_finish = _on_finish(context, model_id, decision.route, destination, user, model)

    if decision.route == CLOUD:
        # Rebuilt from role and text only, so nothing unscanned can reach the provider.
        body = {"model": settings.CLOUD_MODEL, **_sampling_params(req),
                "messages": [{"role": m.get("role"), "content": m.get("content")} for m in req.messages]}
        if settings.CLOUD_MAX_TOKENS_PARAM != "max_tokens":
            body[settings.CLOUD_MAX_TOKENS_PARAM] = body.pop("max_tokens")
        url = f"{settings.CLOUD_API_BASE.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {settings.CLOUD_API_KEY}"}
    else:
        try:
            endpoint = await ensure_model_running(model)
        except Exception as e:
            await on_finish({"status": "failed", "error": str(e)[:500]})
            raise HTTPException(503, "The local model could not be started")
        body = {**_sampling_params(req), "temperature": req.temperature if req.temperature is not None else 0.7,
                "messages": req.messages}
        body["max_tokens"] = await _limit_to_context(endpoint, text, req.token_limit, on_finish)
        url = f"{endpoint}/v1/chat/completions"
        headers = None

    response_headers = _route_headers(decision.route, decision.reason, model_id)
    upstream = _Upstream(url, body, headers, on_finish, model_id, queue_model=model.id if model else None)

    if req.stream:
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
        response = await upstream.stream(req.wants_stream_usage)
        response.headers.update(response_headers)
        return response

    data = await upstream.answer(request)
    data["model"] = model_id
    return JSONResponse(data, headers=response_headers)


# ── Text completions ──────────────────────────────────────────────────────────

@router.post("/completions")
async def completions(
    req:     CompletionRequest,
    request: Request,
    db:      AsyncSession = Depends(get_db),
    user:    User         = Depends(get_openai_user),
):
    _reject_unsupported(req)
    await enforce_user_rpm(user)
    if req.model == MODEL_CLOUD:
        raise HTTPException(400, "Cloud routing is only available on /v1/chat/completions")
    if not isinstance(req.prompt, str):
        raise HTTPException(400, "Only a single string prompt is supported")

    policy = await _load_policy(db)
    model_name = settings.LOCAL_DEFAULT_MODEL if req.model == MODEL_AUTO else req.model
    model = await _resolve_local_model(model_name, user, db)
    context = _audit_context(request, user, req.prompt)
    await db.commit()  # as in chat_completions
    await audit.record_event(
        "inference.routed", **context, model_id=model.id, route="local", destination="local",
        reason="local model requested", sensitive=scan_text(req.prompt, policy.blocked_terms or []), status="routed",
    )
    on_finish = _on_finish(context, model.id, "local", "local", user, model)

    try:
        endpoint = await ensure_model_running(model)
    except Exception as e:
        await on_finish({"status": "failed", "error": str(e)[:500]})
        raise HTTPException(503, "The local model could not be started")

    body = {
        "prompt": req.prompt,
        "n_predict": await _limit_to_context(endpoint, req.prompt, req.token_limit, on_finish),
        "temperature": req.temperature if req.temperature is not None else 0.7,
        "cache_prompt": False,
    }
    if req.top_p is not None:
        body["top_p"] = req.top_p
    if req.seed is not None:
        body["seed"] = req.seed
    if req.stop:
        body["stop"] = [req.stop] if isinstance(req.stop, str) else req.stop
    upstream = _Upstream(f"{endpoint}/completion", body, None, on_finish, model.id,
                         transform=_native_to_openai_completion(model.id, req.stream), queue_model=model.id)
    headers = _route_headers("local", "local model requested", model.id)

    if req.stream:
        body["stream"] = True
        response = await upstream.stream(req.wants_stream_usage)
        response.headers.update(headers)
        return response

    data = await upstream.answer(request)
    data["model"] = model.id
    return JSONResponse(data, headers=headers)
