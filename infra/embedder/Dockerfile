# MeshPilot document reader: text extraction and search vectors
FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

RUN useradd --create-home embedder
USER embedder

EXPOSE 8095
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8095"]
