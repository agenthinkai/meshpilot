#!/bin/bash
# MeshPilot llama.cpp server start script
# Finds the first available GGUF model and starts the server

set -e

MODEL_DIR="${LLAMA_MODEL_DIR:-/models}"
HOST="${LLAMA_HOST:-0.0.0.0}"
PORT="${LLAMA_PORT:-8080}"
THREADS="${LLAMA_THREADS:-4}"
CTX_SIZE="${LLAMA_CTX_SIZE:-4096}"
BATCH_SIZE="${LLAMA_BATCH_SIZE:-512}"

echo "[MeshPilot llama.cpp] Starting server..."
echo "  Model dir:  $MODEL_DIR"
echo "  Threads:    $THREADS"
echo "  Context:    $CTX_SIZE"
echo "  Batch:      $BATCH_SIZE"

# Find first GGUF model
MODEL_PATH=$(find "$MODEL_DIR" -name "*.gguf" | head -1)

if [ -z "$MODEL_PATH" ]; then
    echo "[MeshPilot llama.cpp] WARNING: No GGUF model found in $MODEL_DIR"
    echo "[MeshPilot llama.cpp] Starting in no-model mode (will serve 503 until model is loaded)"
    # Start with a dummy wait loop so the container stays healthy
    while true; do
        MODEL_PATH=$(find "$MODEL_DIR" -name "*.gguf" | head -1)
        if [ -n "$MODEL_PATH" ]; then
            echo "[MeshPilot llama.cpp] Model found: $MODEL_PATH — starting server"
            break
        fi
        sleep 10
    done
fi

echo "[MeshPilot llama.cpp] Loading model: $MODEL_PATH"

exec llama-server \
    --model "$MODEL_PATH" \
    --host "$HOST" \
    --port "$PORT" \
    --threads "$THREADS" \
    --ctx-size "$CTX_SIZE" \
    --batch-size "$BATCH_SIZE" \
    --n-gpu-layers 0 \
    --mlock \
    --log-disable \
    --parallel 4 \
    --cont-batching
