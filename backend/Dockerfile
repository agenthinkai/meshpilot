FROM python:3.11-slim-bookworm

LABEL maintainer="MeshPilot <platform@meshpilot.ai>"
LABEL description="MeshPilot FastAPI backend — CPU-only AI inference platform"

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    libgomp1 \
    libopenblas-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create necessary directories
RUN mkdir -p /data /models /tmp/prometheus

# Non-root user for security
# Also added to the root group (gid 0) so it can read/write the bind-mounted
# docker.sock (owned root:root on the Docker Desktop VM) — needed only for
# the Compare view's live container-memory stats lookup.
RUN useradd -r -s /bin/false -G 0 meshpilot && \
    chown -R meshpilot:meshpilot /app /tmp/prometheus /data /models
USER meshpilot

EXPOSE 8000

# Schema is applied by Alembic before the server starts — the single source
# of truth for schema changes (see backend/alembic/). `exec` hands off PID 1
# to uvicorn so it still receives signals directly for graceful shutdown.
CMD alembic upgrade head && exec uvicorn main:app --host 0.0.0.0 --port 8000 \
    --workers 4 --log-level info --access-log
