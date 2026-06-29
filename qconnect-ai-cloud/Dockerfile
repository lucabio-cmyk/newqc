# =====================================================================
# QConnect-AI Cloud :: root image for the qc_evaluation service.
# ---------------------------------------------------------------------
# Built from the REPO ROOT context so the full `cloud/` package tree
# (config, api/middleware, services) is available and imports resolve via
# the fully-qualified `cloud.*` paths. The per-service Dockerfile under
# cloud/services/qc_evaluation/ is the slim, service-dir-only alternative
# used by docker-compose.
#
# Build:  docker build -t qconnect/qc-evaluation:dev .
# =====================================================================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install the umbrella requirements (covers all services).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the cloud package tree.
COPY cloud ./cloud

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# Run via the fully-qualified module path from the repo root.
CMD ["uvicorn", "cloud.services.qc_evaluation.main:app", "--host", "0.0.0.0", "--port", "8000"]
