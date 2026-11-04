"""Takes turns on a local model, across all API workers.

A model answers PARALLEL_SLOTS requests at once. Without this, the rest wait
inside llama-server, where nobody can tell how long the line is and the wait
counts against INFERENCE_TIMEOUT_S. Requests join in arrival order and each one
works out its own place, so no worker is in charge. A request that disappears
stops renewing its lease and the others remove it.

If Redis is unavailable requests go straight through, as they did before.
"""

from __future__ import annotations

import logging
import time
import uuid

import redis.asyncio as redis

from core.config import settings
from core.model_orchestrator import PARALLEL_SLOTS

log = logging.getLogger(__name__)

_client = redis.from_url(settings.REDIS_URL, socket_timeout=1, socket_connect_timeout=1)

WAITING_LEASE_S = 15
RENEW_EVERY_S = 5


class Ticket:
    def __init__(self, model_id: str):
        self.id = uuid.uuid4().hex
        self.queue = f"mp:queue:{model_id}"
        self.arrived = 0.0
        self.renewed = 0.0
        self.lease_s = WAITING_LEASE_S

    def _lease_key(self, ticket_id: str) -> str:
        return f"{self.queue}:lease:{ticket_id}"

    async def _renew(self) -> None:
        await _client.set(self._lease_key(self.id), 1, ex=self.lease_s)
        self.renewed = time.monotonic()

    async def join(self) -> None:
        try:
            seconds, microseconds = await _client.time()  # one clock for every worker
            self.arrived = seconds + microseconds / 1_000_000
            await self._renew()
            await _client.zadd(self.queue, {self.id: self.arrived})
        except redis.RedisError as e:
            log.warning("request queue unavailable: %s", e)

    async def requests_ahead(self) -> int:
        """How many requests stand between this one and its turn. Zero means go."""
        try:
            await self._renew()
            await _client.zadd(self.queue, {self.id: self.arrived}, nx=True)  # survives eviction
            earlier = await _client.zrangebyscore(self.queue, "-inf", f"({self.arrived}")
            if not earlier:
                return 0
            ids = [i.decode() for i in earlier]
            leases = await _client.mget([self._lease_key(i) for i in ids])
            gone = [i for i, lease in zip(ids, leases) if lease is None]
            if gone:
                await _client.zrem(self.queue, *gone)
            return max(0, len(ids) - len(gone) - PARALLEL_SLOTS + 1)
        except redis.RedisError as e:
            log.warning("request queue unavailable: %s", e)
            return 0

    async def start_turn(self) -> None:
        # Long enough for a silent model server to reach its own timeout.
        self.lease_s = settings.INFERENCE_TIMEOUT_S + 30
        try:
            await self._renew()
        except redis.RedisError as e:
            log.warning("request queue unavailable: %s", e)

    async def still_answering(self) -> None:
        if time.monotonic() - self.renewed >= RENEW_EVERY_S:
            await self.start_turn()

    async def leave(self) -> None:
        try:
            await _client.zrem(self.queue, self.id)
            await _client.delete(self._lease_key(self.id))
        except redis.RedisError as e:
            log.warning("request queue unavailable: %s", e)
