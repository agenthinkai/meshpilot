"""MeshPilot Documents Router — answers questions from uploaded documents.

Admins upload documents. Anyone signed in can ask. Questions always run on a
local model, and the answer comes with the passages it was given to read.
The uploaded file itself isn't kept, only its text and search vectors.
"""

from __future__ import annotations

import json
import os
from contextlib import aclosing
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from core import audit
from core.config import settings
from core.database import Document, DocumentChunk, User, get_db
from core.documents import (
    DocumentReaderUnavailable, build_prompt, chunk_pages, embed, extract_pages, select_passages,
)
from core.model_orchestrator import ensure_model_running
from core.privacy import scan_text
from core.ratelimit import enforce_user_rpm
from core.security import get_current_user_jwt, require_admin
from routers.openai_compat import _audit_context, _limit_to_context, _load_policy, _on_finish, _resolve_local_model, _Upstream

router = APIRouter()

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_ANSWER_TOKENS = 300
ANSWER_TEMPERATURE = 0.1  # answers should read the passages, not get creative
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}


class DocumentOut(BaseModel):
    id:          str
    title:       str
    filename:    str
    chunk_count: int
    created_at:  datetime


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    model:    Optional[str] = None


def _document_out(document: Document) -> DocumentOut:
    return DocumentOut(id=document.id, title=document.title, filename=document.filename,
                       chunk_count=document.chunk_count, created_at=document.created_at)


@router.get("", response_model=List[DocumentOut])
async def list_documents(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user_jwt)):
    result = await db.execute(select(Document).order_by(Document.title))
    return [_document_out(d) for d in result.scalars().all()]


@router.post("", response_model=DocumentOut, status_code=201)
async def upload_document(
    file:  UploadFile   = File(...),
    db:    AsyncSession = Depends(get_db),
    admin: User         = Depends(require_admin),
):
    filename = os.path.basename((file.filename or "").replace("\\", "/"))[:255]
    title, extension = os.path.splitext(filename)
    if extension.lower() not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, "Upload a PDF, Word (.docx) or text file.")
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "This file is over the 25 MB limit.")

    try:
        pages = await extract_pages(data, extension)
        chunks = chunk_pages(pages)
        if not chunks:
            raise ValueError("No readable text found. Scanned documents aren't supported yet.")
        vectors = await embed([f"{title}\n{c.section or ''}\n{c.text}" for c in chunks], "passage")
    except ValueError as e:
        raise HTTPException(422, str(e))
    except DocumentReaderUnavailable:
        raise HTTPException(503, "The document reader isn't available right now.")

    document = Document(title=title, filename=filename, uploaded_by=admin.id, chunk_count=len(chunks))
    db.add(document)
    await db.flush()
    db.add_all(
        DocumentChunk(document_id=document.id, position=n, section=c.section, page=c.page, text=c.text, embedding=v)
        for n, (c, v) in enumerate(zip(chunks, vectors))
    )
    await db.commit()
    await db.refresh(document)
    await audit.record_event(
        "document.uploaded", actor_user_id=admin.id, actor_email=admin.email, status="completed",
        detail={"document_id": document.id, "filename": filename, "passages": len(chunks)},
    )
    return _document_out(document)


@router.delete("/{document_id}", status_code=204)
async def delete_document(document_id: str, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    document = await db.get(Document, document_id)
    if not document:
        raise HTTPException(404, "Document not found")
    filename = document.filename
    await db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document_id))
    await db.delete(document)
    await db.commit()
    await audit.record_event(
        "document.deleted", actor_user_id=admin.id, actor_email=admin.email, status="completed",
        detail={"document_id": document_id, "filename": filename},
    )


async def _find_passages(question: str, db: AsyncSession) -> List[dict]:
    rows = (await db.execute(
        select(DocumentChunk, Document.title).join(Document, Document.id == DocumentChunk.document_id)
    )).all()
    if not rows:
        return []
    question_vector = (await embed([question], "query"))[0]
    passages = [
        {"title": title, "section": c.section, "page": c.page, "text": c.text, "embedding": c.embedding}
        for c, title in rows
    ]
    return select_passages(question, question_vector, passages)


def _source_out(passage: dict) -> dict:
    return {k: passage[k] for k in ("title", "section", "page", "text")}


def _event(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


@router.post("/ask/stream")
async def ask(
    req:     QuestionRequest,
    request: Request,
    db:      AsyncSession = Depends(get_db),
    user:    User         = Depends(get_current_user_jwt),
):
    """Sends the passages first, then the answer as OpenAI-style chunks. If no
    passage matches well enough, says so without running a model at all, since
    a model given poor passages tends to answer anyway."""
    await enforce_user_rpm(user)
    question = req.question.strip()
    policy = await _load_policy(db)
    model = await _resolve_local_model(req.model or settings.DOCUMENTS_DEFAULT_MODEL, user, db)
    try:
        passages = await _find_passages(question, db)
    except DocumentReaderUnavailable:
        raise HTTPException(503, "The document reader isn't available right now.")
    await db.commit()  # gives the connection back, as in chat_completions

    context = _audit_context(request, user, question)
    await audit.record_event(
        "inference.routed", **context, model_id=model.id, route="local", destination="local",
        reason="document question", sensitive=scan_text(question, policy.blocked_terms or []), status="routed",
    )
    on_finish = _on_finish(context, model.id, "local", "local", user, model)

    if not passages:
        await audit.record_event(
            "inference.completed", **context, model_id=model.id, route="local", destination="local",
            status="completed", reason="no matching passage, so no model ran",
        )

        async def not_found():
            yield _event({"sources": [], "not_found": True})
            yield "data: [DONE]\n\n"

        return StreamingResponse(not_found(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    prompt = build_prompt(question, passages)
    try:
        endpoint = await ensure_model_running(model)
    except Exception as e:
        await on_finish({"status": "failed", "error": str(e)[:500]})
        raise HTTPException(503, "The local model could not be started")

    body = {
        "messages": [{"role": "user", "content": prompt}],
        "temperature": ANSWER_TEMPERATURE,
        "max_tokens": await _limit_to_context(endpoint, prompt, MAX_ANSWER_TOKENS, on_finish),
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    upstream = _Upstream(f"{endpoint}/v1/chat/completions", body, None, on_finish, model.id, queue_model=model.id)
    response = await upstream.stream(False)
    answer = response.body_iterator

    async def with_sources():
        yield _event({"sources": [_source_out(p) for p in passages], "not_found": False})
        async with aclosing(answer) as chunks:
            async for chunk in chunks:
                yield chunk

    response.body_iterator = with_sources()
    response.headers["X-MeshPilot-Route"] = "local"
    return response
