"""Typed exception hierarchy shared across QConnect-AI services.

Every application error inherits from :class:`QConnectError`, which carries a
machine-readable ``code`` and an optional ``details`` mapping. API layers can map
these to HTTP responses without leaking stack traces.
"""

from __future__ import annotations

from typing import Any


class QConnectError(Exception):
    """Base class for all QConnect-AI errors.

    Args:
        message: Human readable description.
        code: Stable, machine readable error code (e.g. ``"validation_error"``).
        details: Optional structured context returned to API callers.
    """

    code: str = "qconnect_error"
    http_status: int = 500

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details: dict[str, Any] = details or {}

    def to_dict(self) -> dict[str, Any]:
        """Serialize the error into a JSON-safe payload."""
        return {"error": self.code, "message": self.message, "details": self.details}


class ValidationError(QConnectError):
    """Input failed domain validation (beyond Pydantic's structural checks)."""

    code = "validation_error"
    http_status = 422


class AuthenticationError(QConnectError):
    """Missing, invalid or expired credentials."""

    code = "authentication_error"
    http_status = 401


class AuthorizationError(QConnectError):
    """Authenticated but not permitted to perform the action (RBAC)."""

    code = "authorization_error"
    http_status = 403


class EvaluationError(QConnectError):
    """A QC evaluation engine (Westgard/QConnect/Sigma/ML) failed to compute."""

    code = "evaluation_error"
    http_status = 500


class UpstreamServiceError(QConnectError):
    """A downstream/inter-service call failed or timed out."""

    code = "upstream_service_error"
    http_status = 502


class ConfigurationError(QConnectError):
    """The service is misconfigured (missing env var, bad setting, …)."""

    code = "configuration_error"
    http_status = 500


class NotFoundError(QConnectError):
    """A requested resource does not exist."""

    code = "not_found"
    http_status = 404
