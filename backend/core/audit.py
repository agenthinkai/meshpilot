"""Tamper-evident audit trail.

Each event's HMAC covers its contents and the previous event's HMAC. Editing or
removing a row breaks the chain from that point, and recomputing it needs
AUDIT_HMAC_KEY, which isn't stored in the database. Removing only the newest
rows can't be seen from the chain itself, so verification reports a head hash
that can be kept off this server and compared later.

Prompts are never stored: only a keyed fingerprint and the sensitive-data counts.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from sqlalchemy import select, text

from core.config import settings
from core.database import AsyncSessionLocal, AuditEvent

GENESIS_HASH = "0" * 64
_CHAIN_LOCK_ID = 7203114

_HASHED_FIELDS = (
    "event_type", "actor_user_id", "actor_email", "actor_key_prefix", "request_id", "model_id",
    "route", "destination", "reason", "sensitive", "prompt_fingerprint", "prompt_tokens",
    "completion_tokens", "status", "detail",
)


def prompt_fingerprint(prompt: str) -> str:
    # Keyed, because a plain hash of a short prompt could be reversed by guessing.
    return hmac.new(settings.AUDIT_HMAC_KEY.encode(), prompt.encode(), hashlib.sha256).hexdigest()


def compute_hash(prev_hash: str, created_at: datetime, fields: dict) -> str:
    payload = {name: fields.get(name) for name in _HASHED_FIELDS}
    payload["created_at"] = created_at.astimezone(timezone.utc).isoformat()
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hmac.new(settings.AUDIT_HMAC_KEY.encode(), f"{prev_hash}\n{canonical}".encode(), hashlib.sha256).hexdigest()


async def record_event(event_type: str, **fields) -> None:
    """Commits in its own transaction, so the record survives if the request
    later fails. Errors propagate: callers treat a failed write as a failed
    request rather than doing something that isn't recorded."""
    unknown = set(fields) - set(_HASHED_FIELDS)
    if unknown:
        raise TypeError(f"Unknown audit fields: {sorted(unknown)}")
    fields["event_type"] = event_type

    async with AsyncSessionLocal() as db:
        async with db.begin():
            # Writers across API and worker processes take turns, so the chain stays linear.
            await db.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": _CHAIN_LOCK_ID})
            last = await db.execute(select(AuditEvent.hash).order_by(AuditEvent.seq.desc()).limit(1))
            prev_hash = last.scalar_one_or_none() or GENESIS_HASH
            created_at = datetime.now(timezone.utc)
            db.add(AuditEvent(
                created_at=created_at, prev_hash=prev_hash,
                hash=compute_hash(prev_hash, created_at, fields), **fields,
            ))


async def iter_events(batch_size: int = 1000) -> AsyncIterator[AuditEvent]:
    last_seq = 0
    while True:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(AuditEvent).where(AuditEvent.seq > last_seq).order_by(AuditEvent.seq).limit(batch_size)
            )).scalars().all()
        if not rows:
            return
        for row in rows:
            yield row
        last_seq = rows[-1].seq


async def verify_chain() -> dict:
    checked = 0
    prev_hash = GENESIS_HASH
    first_bad_seq: Optional[int] = None
    async for row in iter_events():
        checked += 1
        fields = {name: getattr(row, name) for name in _HASHED_FIELDS}
        expected = compute_hash(row.prev_hash, row.created_at, fields)
        if row.prev_hash != prev_hash or not hmac.compare_digest(expected, row.hash):
            first_bad_seq = row.seq
            break
        prev_hash = row.hash
    return {
        "ok": first_bad_seq is None,
        "events_checked": checked,
        "first_bad_seq": first_bad_seq,
        "head_hash": prev_hash if first_bad_seq is None else None,
    }
