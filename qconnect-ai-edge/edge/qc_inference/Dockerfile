# =============================================================================
# QConnect-AI Edge - QC Inference service image
# -----------------------------------------------------------------------------
# Build context is the edge repo root (so the whole `edge` package is available
# for cross-service imports). Runtime is dependency-light: pure-Python engines +
# FastAPI. No ML stack baked in (LSTM-lite degrades to a heuristic).
# =============================================================================
FROM python:3.12-slim

# Faster, quieter Python in containers.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy the edge package (engines, cache, hl7, schemas, FastAPI app).
COPY edge /app/edge

# Local cache / model volume.
RUN mkdir -p /data
VOLUME ["/data"]

ENV DB_PATH=/data/qc_cache.db

# 8000: HTTP API + dashboard.  2575: HL7/MLLP listener (when ENABLE_HL7=true).
EXPOSE 8000 2575

# Run the FastAPI app via uvicorn.
CMD ["uvicorn", "edge.qc_inference.main:app", "--host", "0.0.0.0", "--port", "8000"]
