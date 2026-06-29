# =============================================================================
# QConnect-AI Edge - root convenience Dockerfile (qc_inference)
# -----------------------------------------------------------------------------
# Builds the primary inference service from the edge repo root. This is a
# convenience target for `docker build .`; docker-compose uses the per-service
# Dockerfiles under edge/<svc>/Dockerfile.
# =============================================================================
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY edge /app/edge

RUN mkdir -p /data
VOLUME ["/data"]
ENV DB_PATH=/data/qc_cache.db

# 8000: HTTP API + dashboard.  2575: HL7/MLLP listener (when ENABLE_HL7=true).
EXPOSE 8000 2575

CMD ["uvicorn", "edge.qc_inference.main:app", "--host", "0.0.0.0", "--port", "8000"]
