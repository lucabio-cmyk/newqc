"""Correlation-ID and audit-logging middleware (reusable across services).

* :class:`CorrelationIdMiddleware` reads an inbound ``X-Correlation-ID`` header
  (or generates one), stores it in a :class:`contextvars.ContextVar`, binds it to
  the loguru logger for the duration of the request, and echoes it back on the
  response.
* :class:`AuditLoggingMiddleware` emits a structured audit line for every
  request: method, path, status code and wall-clock duration.

These use Starlette's ``BaseHTTPMiddleware`` (re-exported by FastAPI). The
``qc_evaluation`` service imports these directly so the behaviour stays in one
place.
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
except Exception:  # pragma: no cover - allow py_compile without starlette
    BaseHTTPMiddleware = object  # type: ignore
    Request = object  # type: ignore
    Response = object  # type: ignore
    _HAS_STARLETTE = False


CORRELATION_HEADER = "X-Correlation-ID"

# Request-scoped correlation id, readable anywhere in the call stack.
_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def get_correlation_id() -> str | None:
    """Return the correlation id bound to the current request, if any."""
    return _correlation_id.get()


def _new_id() -> str:
    """Generate a fresh hex correlation id."""
    return uuid.uuid4().hex


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Attach a correlation id to every request and propagate it.

    The id comes from the inbound header when present, otherwise it is
    generated. It is exposed via :func:`get_correlation_id`, bound to the
    logger, set on ``request.state.correlation_id`` and returned in the
    response header.
    """

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
    """Emit a structured audit log entry per request.

    Logs method, path, status code and duration in milliseconds. Designed to be
    cheap and to never raise — failures inside the middleware are swallowed so a
    logging hiccup can't take down a request.
    """

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
            except Exception:  # pragma: no cover - never break the response
                pass
