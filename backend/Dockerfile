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
RUN useradd -r -s /bin/false meshpilot && \
    chown -R meshpilot:meshpilot /app /tmp/prometheus
USER meshpilot

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "4", "--log-level", "info", "--access-log"]
