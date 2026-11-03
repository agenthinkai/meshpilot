"""MeshPilot Fit Report Router — test local models on a customer's own tasks."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core import audit
from core.database import FitRun, ModelRecord, ModelStatus, User, UserRole, get_db
from core.fit import score_case
from core.security import get_current_user_jwt

router = APIRouter()

SAMPLE_SUITE_PATH = Path(__file__).resolve().parent.parent / "core" / "fit_sample.json"


class FitCase(BaseModel):
    task:       str = Field(min_length=1, max_length=64)
    prompt:     str = Field(min_length=1, max_length=50_000)
    max_tokens: int = Field(default=200, ge=1, le=2048)
    expect:     dict

    @field_validator("expect")
    @classmethod
    def expect_is_scorable(cls, value: dict) -> dict:
        try:
            score_case("", value)
        except (ValueError, TypeError, AttributeError, IndexError) as e:
            raise ValueError(f"expect can't be scored: {e}")
        return value


class FitWorkload(BaseModel):
    requests_per_day:  int = Field(ge=1)
    avg_input_tokens:  int = Field(ge=1)
    avg_output_tokens: int = Field(ge=1)


class FitSuite(BaseModel):
    name:      str = Field(default="Untitled", max_length=200)
    cases:     List[FitCase] = Field(min_length=1, max_length=200)
    workload:  Optional[FitWorkload] = None
    model_ids: Optional[List[str]] = None


class FitRunSummary(BaseModel):
    id:           str
    name:         str
    status:       str
    progress:     Optional[dict]
    error:        Optional[str]
    created_at:   datetime
    completed_at: Optional[datetime]


class FitRunDetail(FitRunSummary):
    report: Optional[dict]


def _summary(run: FitRun) -> dict:
    return {"id": run.id, "name": run.name, "status": run.status, "progress": run.progress,
            "error": run.error, "created_at": run.created_at, "completed_at": run.completed_at}


async def _get_visible_run(run_id: str, user: User, db: AsyncSession) -> FitRun:
    run = (await db.execute(select(FitRun).where(FitRun.id == run_id))).scalar_one_or_none()
    if not run or (run.user_id != user.id and user.role != UserRole.ADMIN):
        raise HTTPException(404, "Fit run not found")
    return run


@router.get("/sample")
async def sample_suite(user: User = Depends(get_current_user_jwt)):
    return json.loads(SAMPLE_SUITE_PATH.read_text(encoding="utf-8"))


@router.post("/runs", response_model=FitRunSummary, status_code=202)
async def create_run(
    suite: FitSuite,
    db:    AsyncSession = Depends(get_db),
    user:  User         = Depends(get_current_user_jwt),
):
    query = select(ModelRecord).where(
        ModelRecord.status == ModelStatus.READY,
        or_(ModelRecord.is_public == True, ModelRecord.owner_id == user.id),
    )
    if suite.model_ids:
        query = query.where(ModelRecord.id.in_(suite.model_ids))
    models = [m for m in (await db.execute(query)).scalars().all() if "onnx" not in (m.backend or "llamacpp")]
    if not models:
        raise HTTPException(400, "No ready local models to test")

    run = FitRun(
        user_id=user.id, name=suite.name, status="pending",
        suite=suite.model_dump(exclude={"model_ids"}, exclude_none=True),
        model_ids=[m.id for m in models],
        progress={"done": 0, "total": len(models) * len(suite.cases)},
    )
    db.add(run)
    # Commit before dispatching so the worker can always find the row.
    await db.commit()
    await db.refresh(run)

    await audit.record_event(
        "fit_run.started", actor_user_id=user.id, actor_email=user.email, route="local", status="started",
        detail={"run_id": run.id, "models": run.model_ids, "cases": len(suite.cases)},
    )

    from workers.fit_worker import run_fit_task
    run_fit_task.apply_async(args=[run.id], task_id=run.id, queue="inference")
    return _summary(run)


@router.get("/runs", response_model=List[FitRunSummary])
async def list_runs(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user_jwt)):
    query = select(FitRun).order_by(desc(FitRun.created_at)).limit(50)
    if user.role != UserRole.ADMIN:
        query = query.where(FitRun.user_id == user.id)
    return [_summary(r) for r in (await db.execute(query)).scalars().all()]


@router.get("/runs/{run_id}", response_model=FitRunDetail)
async def get_run(run_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user_jwt)):
    run = await _get_visible_run(run_id, user, db)
    return {**_summary(run), "report": run.report}


@router.delete("/runs/{run_id}", status_code=204)
async def delete_run(run_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user_jwt)):
    """Removes the customer's test documents and outputs. The audit record that
    the run happened stays, and it never contained the documents."""
    run = await _get_visible_run(run_id, user, db)
    if run.status in ("pending", "running"):
        raise HTTPException(409, "Wait for this run to finish before deleting it")
    await audit.record_event("fit_run.deleted", actor_user_id=user.id, actor_email=user.email,
                             status="deleted", detail={"run_id": run.id})
    await db.delete(run)
