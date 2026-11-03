FROM python:3.11-slim-bookworm

LABEL description="MeshPilot 'without MeshPilot' baseline — plain PyTorch/transformers CPU inference, for live Compare demos"

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

EXPOSE 8091

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8091"]
