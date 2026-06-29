# QC-Evaluation service image.
# Build context: ./cloud/services/qc_evaluation (the service directory only).
# The service is self-contained: it vendors local copies of the middleware and
# settings so it does not need the rest of the monorepo on the path.
FROM python:3.12-slim AS base

# Avoid .pyc files and buffer flushing surprises in containers.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System deps: curl for the container healthcheck; build tools for scipy/numpy
# wheels are usually unnecessary on slim+manylinux but kept minimal here.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the service source (build context is the service directory).
COPY . ./

# Drop privileges.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# main:app — uvicorn run from the service directory (which is /app).
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
