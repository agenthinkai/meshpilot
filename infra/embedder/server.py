"""Reads uploaded documents and turns text into vectors for document search.

It holds no data and can't reach the database. It stays inside the Docker
network, and only the API calls it. The model file comes from the model volume,
so nothing is downloaded at runtime.
"""

import io
import os
from typing import List, Literal

import numpy as np
import onnxruntime as ort
from docx import Document
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from pypdf import PdfReader
from tokenizers import Tokenizer

MODEL_DIR = os.environ.get("EMBEDDER_MODEL_DIR", "/models/multilingual-e5-small")
MAX_TOKENS = 512
BATCH_SIZE = 16

app = FastAPI(title="MeshPilot document reader", docs_url=None, redoc_url=None, openapi_url=None)

_options = ort.SessionOptions()
_options.intra_op_num_threads = int(os.environ.get("EMBEDDER_THREADS", "2"))
_session = ort.InferenceSession(os.path.join(MODEL_DIR, "model.onnx"), _options, providers=["CPUExecutionProvider"])
_inputs = {i.name for i in _session.get_inputs()}
_tokenizer = Tokenizer.from_file(os.path.join(MODEL_DIR, "tokenizer.json"))
_tokenizer.enable_truncation(MAX_TOKENS)
_tokenizer.enable_padding()


class EmbedRequest(BaseModel):
    texts: List[str] = Field(min_length=1, max_length=2000)
    kind:  Literal["query", "passage"]


def _embed_batch(texts: List[str]) -> np.ndarray:
    encoded = _tokenizer.encode_batch(texts)
    ids = np.array([e.ids for e in encoded], dtype=np.int64)
    mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
    feed = {"input_ids": ids, "attention_mask": mask}
    if "token_type_ids" in _inputs:
        feed["token_type_ids"] = np.zeros_like(ids)
    hidden = _session.run(None, feed)[0]
    weights = mask[..., None]
    vectors = (hidden * weights).sum(axis=1) / weights.sum(axis=1)
    return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/embed")
def embed(req: EmbedRequest):
    # This model expects its text labelled as a query or a passage.
    texts = [f"{req.kind}: {t}" for t in req.texts]
    vectors = [_embed_batch(texts[i:i + BATCH_SIZE]) for i in range(0, len(texts), BATCH_SIZE)]
    return {"vectors": np.round(np.vstack(vectors), 5).tolist()}


def _read_pdf(data: bytes) -> List[dict]:
    return [{"page": n, "text": page.extract_text() or ""} for n, page in enumerate(PdfReader(io.BytesIO(data)).pages, 1)]


def _read_docx(data: bytes) -> List[dict]:
    document = Document(io.BytesIO(data))
    lines = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            lines.append(" | ".join(cell.text.strip() for cell in row.cells))
    return [{"page": None, "text": "\n".join(lines)}]


def _read_text(data: bytes) -> List[dict]:
    for encoding in ("utf-8-sig", "cp1256"):
        try:
            return [{"page": None, "text": data.decode(encoding)}]
        except UnicodeDecodeError:
            continue
    raise ValueError("unknown text encoding")


READERS = {".pdf": _read_pdf, ".docx": _read_docx, ".txt": _read_text, ".md": _read_text}


@app.post("/extract")
async def extract(request: Request, extension: str):
    reader = READERS.get(extension.lower())
    if not reader:
        raise HTTPException(400, "Unsupported file type")
    try:
        return {"pages": reader(await request.body())}
    except Exception:
        raise HTTPException(422, "This file could not be read")
