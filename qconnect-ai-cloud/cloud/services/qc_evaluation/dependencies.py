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


# RBAC helper (guarded so the module still compiles without the security pkg).
try:
    from cloud.config.security import has_role
except Exception:  # pragma: no cover - fallback layouts
    try:
        from security import has_role  # type: ignore
    except Exception:

        def has_role(user_role: Any, required_role: str) -> bool:  # type: ignore
            """Permissive fallback when the security module is unavailable."""
            return True


# A header-reading sub-dependency. Defined conditionally so importing this
# module never requires FastAPI (Header(...) cannot be evaluated otherwise).
if _HAS_FASTAPI:

    def _auth_header(
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> str | None:
        """Extract the raw ``Authorization`` header value (or None)."""
        return authorization

else:  # pragma: no cover - minimal env without FastAPI

    def _auth_header() -> str | None:
        return None


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


def _forbidden(detail: str) -> None:
    """Raise an HTTP 403 (or a plain error if FastAPI is absent)."""
    if _HAS_FASTAPI and status is not None:
        raise HTTPException(  # type: ignore[misc]
            status_code=status.HTTP_403_FORBIDDEN,
            detail=detail,
        )
    raise AuthError(detail)


# --------------------------------------------------------------------------- #
# RBAC dependencies (FastAPI-facing; defined only when FastAPI is available so
# the module still compiles in a minimal environment).
# --------------------------------------------------------------------------- #
if _HAS_FASTAPI:

    async def get_principal(
        authorization: str | None = Depends(_auth_header),
    ) -> dict[str, Any] | None:
        """FastAPI dependency: decode the bearer token from the request header.

        Unlike :func:`get_current_principal` (which takes the raw value and is
        unit-testable in isolation), this reads the ``Authorization`` header via
        a sub-dependency so the token is actually extracted from the request.
        """
        return await get_current_principal(authorization)

    def require_role(required_role: str):
        """Return a dependency enforcing ``required_role`` (hierarchical RBAC).

        Behaviour:
            * ``require_auth`` False (dev/test default) -> bypass; the principal
              (possibly ``None``) is returned without an RBAC check.
            * ``require_auth`` True -> a valid token is mandatory (401 if absent)
              and its role must meet/exceed ``required_role`` (403 otherwise).
        """

        async def _dependency(
            principal: dict[str, Any] | None = Depends(get_principal),
        ) -> dict[str, Any] | None:
            settings = get_settings()
            if not bool(getattr(settings, "require_auth", False)):
                return principal  # dev/test bypass
            if principal is None:
                _unauthorized("Authentication required")
            if not has_role(principal.get("role"), required_role):
                _forbidden(f"Requires role '{required_role}'")
            return principal

        return _dependency

else:  # pragma: no cover - minimal env without FastAPI

    def require_role(required_role: str):
        """No-op RBAC factory when FastAPI is unavailable."""

        async def _dependency() -> None:
            return None

        return _dependency
