# =============================================================================
# QConnect-AI Edge - Data Sync daemon image
# -----------------------------------------------------------------------------
# Drains the local cache to the cloud and pulls model/limit updates. Shares the
# `edge` package with the inference service (build context is the edge repo root).
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

# Run the sync daemon. PYTHONPATH lets `python -m data_sync.run` resolve while
# the code itself imports the absolute `edge.*` packages.
CMD ["python", "-m", "edge.data_sync.run"]
