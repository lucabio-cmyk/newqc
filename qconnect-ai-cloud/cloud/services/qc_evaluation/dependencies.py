"""FastAPI dependency-injection providers for the QC-Evaluation service.

These are deliberately resilient: ``get_db`` yields ``None`` when no database is
configured/available (so engine-only code paths and offline tests work), and the
auth dependency is *optional* unless ``settings.require_auth`` is True.

This module is imported both as ``dependencies`` (when the service directory is
on ``sys.path``, e.g. inside the container / tests) and is self-contained enough
to compile standalone. The settings/security imports are guarded so importing it
never hard-fails in a minimal environment.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from loguru import logger

try:
    from fastapi import Depends, Header, HTTPException, status

    _HAS_FASTAPI = True
except Exception:  # pragma: no cover - py_compile without fastapi
    Depends = None  # type: ignore
    Header = None  # type: ignore
    HTTPException = Exception  # type: ignore
    status = None  # type: ignore
    _HAS_FASTAPI = False

# Settings / security are pulled from the shared cloud config when importable.
try:
    from cloud.config.security import AuthError, decode_jwt
    from cloud.config.settings import Settings, get_settings
except Exception:  # pragma: no cover - allow standalone import in container
    try:
        # Fallback for the service-dir-only container layout: a local settings
        # module ships alongside this file; JWT decoding degrades to a stub if
        # the security helpers are unavailable.
        from settings import Settings, get_settings  # type: ignore

        try:
            from cloud.config.security import AuthError, decode_jwt  # type: ignore
        except Exception:

            class AuthError(Exception):  # type: ignore
                """Fallback auth error."""

            def decode_jwt(*_a: Any, **_k: Any) -> dict:  # type: ignore
                raise AuthError("JWT support unavailable")

    except Exception:
        Settings = Any  # type: ignore

        def get_settings() -> Any:  # type: ignore
            """Last-resort settings stub returning ``None``."""
            return None

        class AuthError(Exception):  # type: ignore
            """Fallback auth error."""

        def decode_jwt(*_a: Any, **_k: Any) -> dict:  # type: ignore
            raise AuthError("JWT support unavailable")


async def get_db() -> AsyncIterator[Any]:
    """Yield an async DB session, or ``None`` when no DB is configured.

    Implemented as an async generator so FastAPI handles set-up/tear-down. If
    SQLAlchemy or a live database is unavailable the dependency yields ``None``
    and callers must treat persistence as best-effort.
    """
    sessionmaker = None
    try:
        from cloud.config.database import get_sessionmaker

        sessionmaker = get_sessionmaker()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("get_db: sessionmaker unavailable: {}", exc)

    if sessionmaker is None:
        # No DB configured — yield None so endpoints degrade gracefully.
        yield None
        return

    session = sessionmaker()
    try:
        yield session
    except Exception:
        try:
            await session.rollback()
        except Exception:  # pragma: no cover
            pass
        raise
    finally:
        try:
            await session.close()
        except Exception:  # pragma: no cover
            pass


def get_app_settings() -> Any:
    """Return the cached application settings (FastAPI dependency)."""
    return get_settings()


async def get_current_principal(
    authorization: str | None = None,
) -> dict[str, Any] | None:
    """Optional bearer-token authentication.

    Returns the decoded JWT claims when a valid ``Authorization: Bearer <jwt>``
    header is present. When ``settings.require_auth`` is False (development) a
    missing token yields ``None`` instead of raising. When auth is required, a
    missing or invalid token raises HTTP 401.

    Note:
        FastAPI injects the ``Authorization`` header via ``Header(...)`` in the
        wiring done in ``main.py``; here we accept the raw string so the function
        is also unit-testable in isolation.
    """
    settings = get_settings()
    require_auth = bool(getattr(settings, "require_auth", False))

    if not authorization:
        if require_auth:
            _unauthorized("Missing Authorization header")
        return None

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        if require_auth:
            _unauthorized("Authorization header must be 'Bearer <token>'")
        return None

    try:
        secret = getattr(settings, "jwt_secret", "")
        algorithm = getattr(settings, "jwt_algorithm", "HS256")
        claims = decode_jwt(token, secret, algorithms=[algorithm])
        return claims
    except AuthError as exc:
        if require_auth:
            _unauthorized(str(exc))
        logger.debug("Ignoring invalid token in dev mode: {}", exc)
        return None


def _unauthorized(detail: str) -> None:
    """Raise an HTTP 401 (or a plain error if FastAPI is absent)."""
    if _HAS_FASTAPI and status is not None:
        raise HTTPException(  # type: ignore[misc]
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )
    raise AuthError(detail)
