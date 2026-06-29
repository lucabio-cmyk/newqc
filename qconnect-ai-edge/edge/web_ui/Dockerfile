# =============================================================================
# QConnect-AI Edge - Web UI (Flask) image
# -----------------------------------------------------------------------------
# Operator dashboard. Reads the same local SQLite cache as the inference service
# (build context is the edge repo root so `edge.qc_inference.cache` is importable).
# =============================================================================
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY edge /app/edge

ENV DB_PATH=/data/qc_cache.db \
    WEB_UI_PORT=8001

EXPOSE 8001

# Flask's built-in server is fine for a single-lab internal dashboard. Swap for
# gunicorn (`gunicorn -b 0.0.0.0:8001 edge.web_ui.app:app`) for higher load.
CMD ["python", "-m", "edge.web_ui.app"]
