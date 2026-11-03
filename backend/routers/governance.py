"""MeshPilot Governance Router — privacy policy and audit trail (admin-only)."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core import audit
from core.config import settings
from core.database import AuditEvent, PrivacyPolicy, User, get_db
from core.privacy import ON_SENSITIVE_BLOCK, ON_SENSITIVE_LOCAL, scan_text
from core.security import require_admin

router = APIRouter()


class PolicyOut(BaseModel):
    cloud_enabled:        bool
    on_sensitive:         str
    blocked_terms:        List[str]
    auto_cloud_min_chars: Optional[int]
    updated_at:           Optional[datetime]
    updated_by:           Optional[str]
    # Set by the server's environment; admins can't change these from the dashboard.
    cloud_server_switch:  bool
    cloud_configured:     bool
    cloud_destination:    Optional[str]
    local_default_model:  str


class PolicyUpdate(BaseModel):
    cloud_enabled:        bool
    on_sensitive:         str = Field(pattern=f"^({ON_SENSITIVE_LOCAL}|{ON_SENSITIVE_BLOCK})$")
    blocked_terms:        List[str] = Field(default_factory=list, max_length=500)
    auto_cloud_min_chars: Optional[int] = Field(default=None, ge=1)


class ScanRequest(BaseModel):
    text: str = Field(max_length=200_000)


class AuditEventOut(BaseModel):
    seq:                int
    created_at:         datetime
    event_type:         str
    actor_email:        Optional[str]
    actor_key_prefix:   Optional[str]
    request_id:         Optional[str]
    model_id:           Optional[str]
    route:              Optional[str]
    destination:        Optional[str]
    reason:             Optional[str]
    sensitive:          Optional[dict]
    prompt_fingerprint: Optional[str]
    prompt_tokens:      Optional[int]
    completion_tokens:  Optional[int]
    status:             Optional[str]
    detail:             Optional[dict]
    prev_hash:          str
    hash:               str


EXPORT_FIELDS = list(AuditEventOut.model_fields)


def _policy_out(policy: PrivacyPolicy) -> PolicyOut:
    return PolicyOut(
        cloud_enabled=policy.cloud_enabled, on_sensitive=policy.on_sensitive,
        blocked_terms=policy.blocked_terms or [], auto_cloud_min_chars=policy.auto_cloud_min_chars,
        updated_at=policy.updated_at, updated_by=policy.updated_by,
        cloud_server_switch=settings.CLOUD_ROUTING_ENABLED,
        cloud_configured=bool(settings.CLOUD_API_BASE and settings.CLOUD_API_KEY and settings.CLOUD_MODEL),
        cloud_destination=settings.CLOUD_API_BASE or None,
        local_default_model=settings.LOCAL_DEFAULT_MODEL,
    )


@router.get("/policy", response_model=PolicyOut)
async def get_policy(db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    return _policy_out((await db.execute(select(PrivacyPolicy).where(PrivacyPolicy.id == 1))).scalar_one())


@router.put("/policy", response_model=PolicyOut)
async def update_policy(
    body:  PolicyUpdate,
    db:    AsyncSession = Depends(get_db),
    admin: User         = Depends(require_admin),
):
    policy = (await db.execute(select(PrivacyPolicy).where(PrivacyPolicy.id == 1))).scalar_one()
    before = {"cloud_enabled": policy.cloud_enabled, "on_sensitive": policy.on_sensitive,
              "blocked_terms": len(policy.blocked_terms or []), "auto_cloud_min_chars": policy.auto_cloud_min_chars}
    terms = sorted({t.strip() for t in body.blocked_terms if t.strip()})
    after = {"cloud_enabled": body.cloud_enabled, "on_sensitive": body.on_sensitive,
             "blocked_terms": len(terms), "auto_cloud_min_chars": body.auto_cloud_min_chars}

    # Recorded first: a policy change that can't be recorded doesn't happen.
    # Blocked terms are logged as a count, since they can name customers or projects.
    await audit.record_event("policy.updated", actor_user_id=admin.id, actor_email=admin.email,
                             status="updated", detail={"before": before, "after": after})

    policy.cloud_enabled = body.cloud_enabled
    policy.on_sensitive = body.on_sensitive
    policy.blocked_terms = terms
    policy.auto_cloud_min_chars = body.auto_cloud_min_chars
    policy.updated_at = datetime.now(timezone.utc)
    policy.updated_by = admin.email
    return _policy_out(policy)


@router.post("/scan")
async def try_scan(
    body:  ScanRequest,
    db:    AsyncSession = Depends(get_db),
    admin: User         = Depends(require_admin),
):
    """Shows what the gateway would detect in a piece of text. The text isn't stored."""
    policy = (await db.execute(select(PrivacyPolicy).where(PrivacyPolicy.id == 1))).scalar_one()
    return {"sensitive": scan_text(body.text, policy.blocked_terms or [])}


@router.get("/audit/events", response_model=List[AuditEventOut])
async def list_events(
    limit:      int           = Query(default=100, ge=1, le=500),
    before_seq: Optional[int] = None,
    route:      Optional[str] = None,
    event_type: Optional[str] = None,
    db:         AsyncSession  = Depends(get_db),
    admin:      User          = Depends(require_admin),
):
    query = select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(limit)
    if before_seq is not None:
        query = query.where(AuditEvent.seq < before_seq)
    if route:
        query = query.where(AuditEvent.route == route)
    if event_type:
        query = query.where(AuditEvent.event_type == event_type)
    rows = (await db.execute(query)).scalars().all()
    return [AuditEventOut.model_validate(r, from_attributes=True) for r in rows]


@router.get("/audit/summary")
async def audit_summary(db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    routed = AuditEvent.event_type.in_(["inference.routed", "inference.blocked"])
    rows = (await db.execute(
        select(AuditEvent.route, func.count()).where(routed).group_by(AuditEvent.route)
    )).all()
    counts = {route: count for route, count in rows}
    sensitive_rows = (await db.execute(
        select(AuditEvent.sensitive).where(routed, AuditEvent.route != "cloud")
    )).scalars().all()
    return {
        "local": counts.get("local", 0),
        "cloud": counts.get("cloud", 0),
        "blocked": counts.get("blocked", 0),
        "with_sensitive_data": sum(1 for s in sensitive_rows if s),
    }


@router.get("/audit/verify")
async def verify_audit(admin: User = Depends(require_admin)):
    return await audit.verify_chain()


def _csv_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    text = json.dumps(value, sort_keys=True) if isinstance(value, dict) else str(value)
    # A leading =, +, - or @ would run as a formula when opened in a spreadsheet.
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


@router.get("/audit/export")
async def export_audit(
    format: str  = Query(default="csv", pattern="^(csv|jsonl)$"),
    admin:  User = Depends(require_admin),
):
    await audit.record_event("audit.exported", actor_user_id=admin.id, actor_email=admin.email,
                             status="exported", detail={"format": format})

    async def rows():
        if format == "csv":
            buffer = io.StringIO()
            csv.writer(buffer).writerow(EXPORT_FIELDS)
            yield buffer.getvalue()
        async for event in audit.iter_events():
            if format == "jsonl":
                yield AuditEventOut.model_validate(event, from_attributes=True).model_dump_json() + "\n"
            else:
                buffer = io.StringIO()
                csv.writer(buffer).writerow([_csv_cell(getattr(event, name)) for name in EXPORT_FIELDS])
                yield buffer.getvalue()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return StreamingResponse(
        rows(), media_type="text/csv" if format == "csv" else "application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="meshpilot-audit-{stamp}.{format}"'},
    )
