"""Local (vendored) correlation-id + audit middleware for the QC service.

This is a self-contained copy of :mod:`cloud.api.middleware.correlation` so the
qc_evaluation Docker image — whose build context is only the service directory —
can run without the rest of the monorepo on the path. In the monorepo / test
layout the shared version under ``cloud/api/middleware`` is preferred; this file
is the container fallback. Keep the two in sync.
"""

from __future__ import annotations

import time
import uuid
from contextvars import ContextVar

from loguru import logger

try:
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import Response

    _HAS_STARLETTE = True
except Exception:  # pragma: no cover
    BaseHTTPMiddleware = object  # type: ignore
    Request = object  # type: ignore
    Response = object  # type: ignore
    _HAS_STARLETTE = False


CORRELATION_HEADER = "X-Correlation-ID"

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def get_correlation_id() -> str | None:
    """Return the correlation id bound to the current request, if any."""
    return _correlation_id.get()


def _new_id() -> str:
    """Generate a fresh hex correlation id."""
    return uuid.uuid4().hex


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Attach/propagate a correlation id for every request."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        incoming = request.headers.get(CORRELATION_HEADER)
        corr_id = incoming or _new_id()
        token = _correlation_id.set(corr_id)
        request.state.correlation_id = corr_id
        try:
            with logger.contextualize(correlation_id=corr_id):
                response = await call_next(request)
        finally:
            _correlation_id.reset(token)
        response.headers[CORRELATION_HEADER] = corr_id
        return response


class AuditLoggingMiddleware(BaseHTTPMiddleware):
    """Emit a structured audit log line per request (method/path/status/ms)."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration_ms = (time.perf_counter() - start) * 1000.0
            try:
                logger.bind(
                    method=request.method,
                    path=request.url.path,
                    status=status_code,
                    duration_ms=round(duration_ms, 2),
                    client=getattr(request.client, "host", None),
                ).info(
                    "audit {} {} -> {} ({:.2f} ms)",
                    request.method,
                    request.url.path,
                    status_code,
                    duration_ms,
                )
            except Exception:  # pragma: no cover
                pass
