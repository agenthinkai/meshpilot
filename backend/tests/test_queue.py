import asyncio

import pytest
from fastapi import HTTPException

from routers import openai_compat
from routers.openai_compat import _Upstream


class AlwaysBehindTicket:
    def __init__(self):
        self.left = False

    async def join(self):
        pass

    async def requests_ahead(self):
        return 2

    async def start_turn(self):
        raise AssertionError("this request never gets a turn")

    async def leave(self):
        self.left = True


class ClientStaysConnected:
    async def is_disconnected(self):
        return False


def waiting_request(monkeypatch, max_wait_s):
    monkeypatch.setattr(openai_compat.settings, "QUEUE_MAX_WAIT_S", max_wait_s)
    monkeypatch.setattr(openai_compat, "QUEUE_POLL_S", 0.01)
    outcomes = []

    async def on_finish(result):
        outcomes.append(result)

    upstream = _Upstream("http://unused", {}, None, on_finish, "model")
    upstream.ticket = AlwaysBehindTicket()
    return upstream, outcomes


def test_waiting_too_long_without_streaming_says_busy(monkeypatch):
    upstream, outcomes = waiting_request(monkeypatch, 0)

    with pytest.raises(HTTPException) as refused:
        asyncio.run(upstream.answer(ClientStaysConnected()))

    assert refused.value.status_code == 503 and refused.value.headers["Retry-After"]
    assert [o["status"] for o in outcomes] == ["failed"] and upstream.ticket.left


def test_waiting_too_long_while_streaming_says_busy(monkeypatch):
    upstream, outcomes = waiting_request(monkeypatch, 0)

    async def read():
        return [chunk async for chunk in upstream._wait_then_relay(2, False)]

    chunks = asyncio.run(read())
    assert chunks[0] == ": queue ahead=2\n\n" and "busy" in chunks[-1]
    assert [o["status"] for o in outcomes] == ["failed"] and upstream.ticket.left


def test_leaving_the_line_gives_up_the_place(monkeypatch):
    upstream, outcomes = waiting_request(monkeypatch, 60)

    async def read_then_leave():
        chunks = upstream._wait_then_relay(2, False)
        assert await chunks.__anext__() == ": queue ahead=2\n\n"
        await chunks.aclose()
        await asyncio.gather(*openai_compat._background_tasks)

    asyncio.run(read_then_leave())
    assert outcomes[0]["error"] == "left the queue before its turn" and upstream.ticket.left
