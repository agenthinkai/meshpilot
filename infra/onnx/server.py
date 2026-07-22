"""
MeshPilot ONNX Runtime Inference Server
Serves ONNX models via FastAPI with OpenVINO EP fallback.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import onnxruntime as ort
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="MeshPilot ONNX Server")

# Session cache: model_path → ort.InferenceSession
_sessions: Dict[str, ort.InferenceSession] = {}


def _get_providers():
    available = ort.get_available_providers()
    # Prefer OpenVINO EP if available
    if "OpenVINOExecutionProvider" in available:
        return ["OpenVINOExecutionProvider", "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


def _load_session(model_path: str) -> ort.InferenceSession:
    if model_path not in _sessions:
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = int(os.environ.get("ORT_NUM_THREADS", 4))
        opts.inter_op_num_threads = int(os.environ.get("ORT_INTER_OP_THREADS", 2))
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        opts.execution_mode = ort.ExecutionMode.ORT_PARALLEL
        _sessions[model_path] = ort.InferenceSession(
            model_path, sess_options=opts, providers=_get_providers()
        )
    return _sessions[model_path]


class InferRequest(BaseModel):
    model_path:  str
    prompt:      str
    max_tokens:  int   = 512
    temperature: float = 0.7


class InferResponse(BaseModel):
    text:             str
    tokens_generated: int
    prompt_tokens:    int
    latency_ms:       float
    provider_used:    str


@app.get("/health")
def health():
    return {"status": "ok", "providers": ort.get_available_providers()}


@app.post("/infer", response_model=InferResponse)
def infer(req: InferRequest):
    if not Path(req.model_path).exists():
        raise HTTPException(404, f"Model not found: {req.model_path}")

    try:
        session = _load_session(req.model_path)
    except Exception as e:
        raise HTTPException(500, f"Failed to load model: {e}")

    # Tokenize (simple whitespace tokenization for demo;
    # production would use the model's tokenizer)
    tokens = req.prompt.split()
    prompt_len = len(tokens)

    # Build input tensor — shape depends on model
    # This is a generic fallback; specific models need their own input prep
    input_ids = np.array([[hash(t) % 32000 for t in tokens]], dtype=np.int64)

    inputs = session.get_inputs()
    input_name = inputs[0].name

    t0 = time.perf_counter()
    try:
        outputs = session.run(None, {input_name: input_ids})
        latency_ms = (time.perf_counter() - t0) * 1000

        # Extract logits and decode (simplified)
        logits = outputs[0]
        if logits.ndim == 3:
            next_token_logits = logits[0, -1, :]
        elif logits.ndim == 2:
            next_token_logits = logits[0]
        else:
            next_token_logits = logits.flatten()

        # Greedy decode for demo
        generated_ids = []
        for _ in range(min(req.max_tokens, 50)):
            next_id = int(np.argmax(next_token_logits))
            generated_ids.append(next_id)
            if next_id == 2:  # EOS
                break

        text = f"[ONNX output: {len(generated_ids)} tokens generated from {prompt_len} prompt tokens]"
        provider = session.get_providers()[0]

        return InferResponse(
            text=text,
            tokens_generated=len(generated_ids),
            prompt_tokens=prompt_len,
            latency_ms=round(latency_ms, 1),
            provider_used=provider,
        )
    except Exception as e:
        raise HTTPException(500, f"Inference error: {e}")


@app.delete("/models/{model_key}")
def unload_model(model_key: str):
    """Unload a cached model session to free memory."""
    removed = [k for k in list(_sessions.keys()) if model_key in k]
    for k in removed:
        del _sessions[k]
    return {"unloaded": len(removed)}
