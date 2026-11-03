"""MeshPilot 'without MeshPilot' baseline server.

Loads a model once at startup with plain transformers/PyTorch (no quantization,
no llama.cpp) and serves it over HTTP. This exists purely so the dashboard's
Compare view can show a genuinely live 'without MeshPilot' response next to
the real MeshPilot (llama.cpp) response, instead of a canned/precomputed number.
"""
import logging
import time
from contextlib import asynccontextmanager

import torch
from fastapi import FastAPI
from pydantic import BaseModel
from transformers import AutoModelForCausalLM, AutoTokenizer
import os
import anyio

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pytorch-baseline")

MODEL_DIR = os.environ.get("BASELINE_MODEL_DIR", "/baseline_model")
THREADS = int(os.environ.get("BASELINE_THREADS", "4"))

state = {"tokenizer": None, "model": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    torch.set_num_threads(THREADS)
    logger.info(f"Loading baseline model from {MODEL_DIR} (FP32, plain transformers, CPU)...")
    state["tokenizer"] = AutoTokenizer.from_pretrained(MODEL_DIR)
    state["model"] = AutoModelForCausalLM.from_pretrained(MODEL_DIR, torch_dtype=torch.float32)
    state["model"].eval()
    logger.info("Baseline model loaded.")
    yield


app = FastAPI(title="MeshPilot Baseline (no MeshPilot)", lifespan=lifespan)


class GenerateRequest(BaseModel):
    prompt: str
    max_tokens: int = 256
    temperature: float = 0.0
    raw_prompt: bool = False  # prompt is already formatted; skip the chat template


@app.get("/health")
async def health():
    return {"status": "ok", "model_loaded": state["model"] is not None}


def _generate_sync(prompt: str, max_tokens: int, raw_prompt: bool = False) -> dict:
    tokenizer = state["tokenizer"]
    model = state["model"]
    if raw_prompt:
        input_text = prompt
    else:
        messages = [{"role": "user", "content": prompt}]
        input_text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    inputs = tokenizer(input_text, return_tensors="pt")
    prompt_tokens = inputs["input_ids"].shape[1]

    t0 = time.perf_counter()
    with torch.no_grad():
        out = model.generate(
            **inputs,
            max_new_tokens=max_tokens,
            do_sample=False,
            temperature=None,
            top_p=None,
        )
    latency_ms = (time.perf_counter() - t0) * 1000
    completion_tokens = out.shape[1] - prompt_tokens
    text = tokenizer.decode(out[0][prompt_tokens:], skip_special_tokens=True)
    tps = completion_tokens / (latency_ms / 1000) if latency_ms > 0 else 0
    return {
        "text": text,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "latency_ms": round(latency_ms, 1),
        "throughput_tps": round(tps, 2),
        "backend_used": "pytorch_cpu_fp32",
    }


@app.post("/generate")
async def generate(req: GenerateRequest):
    return await anyio.to_thread.run_sync(_generate_sync, req.prompt, req.max_tokens, req.raw_prompt)
