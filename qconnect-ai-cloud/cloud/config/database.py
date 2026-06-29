"""Async SQLAlchemy engine / session factory.

The factory is **lazy and guarded**: importing this module never opens a
connection, and if SQLAlchemy (or the async driver) is unavailable the helpers
degrade to ``None`` so that pure-engine code paths and offline unit tests keep
working without a database.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from cloud.config.settings import get_settings

try:
    from sqlalchemy.ext.asyncio import (
        AsyncEngine,
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )
    from sqlalchemy.orm import DeclarativeBase

    _HAS_SQLALCHEMY = True
except Exception:  # pragma: no cover - minimal env without sqlalchemy
    AsyncEngine = Any  # type: ignore
    AsyncSession = Any  # type: ignore
    async_sessionmaker = None  # type: ignore
    create_async_engine = None  # type: ignore
    DeclarativeBase = object  # type: ignore
    _HAS_SQLALCHEMY = False


if _HAS_SQLALCHEMY:

    class Base(DeclarativeBase):
        """Declarative base for ORM models (kept minimal here)."""

else:  # pragma: no cover

    class Base:  # type: ignore
        """Placeholder base when SQLAlchemy is not installed."""


# Module-level singletons created on first use.
_engine: AsyncEngine | None = None
_sessionmaker: Any | None = None


def get_engine() -> AsyncEngine | None:
    """Return (creating once) the async engine, or ``None`` if unavailable.

    Errors during engine creation are swallowed and logged so a missing/invalid
    DSN never crashes service start-up; callers must handle a ``None`` engine.
    """
    global _engine
    if not _HAS_SQLALCHEMY:
        return None
    if _engine is None:
        settings = get_settings()
        try:
            _engine = create_async_engine(
                settings.database_url,
                pool_pre_ping=True,
                future=True,
            )
            logger.info("Async DB engine created for {}", _redacted(settings.database_url))
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Could not create DB engine: {}", exc)
            _engine = None
    return _engine


def get_sessionmaker() -> Any | None:
    """Return (creating once) an ``async_sessionmaker`` bound to the engine."""
    global _sessionmaker
    if not _HAS_SQLALCHEMY:
        return None
    if _sessionmaker is None:
        engine = get_engine()
        if engine is None:
            return None
        _sessionmaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return _sessionmaker


async def dispose_engine() -> None:
    """Dispose the engine connection pool on shutdown (best-effort)."""
    global _engine
    if _engine is not None:
        try:
            await _engine.dispose()
            logger.info("DB engine disposed")
        except Exception as exc:  # pragma: no cover
            logger.warning("Error disposing engine: {}", exc)
        finally:
            _engine = None


def _redacted(dsn: str) -> str:
    """Hide credentials when logging a DSN."""
    if "@" in dsn and "://" in dsn:
        scheme, rest = dsn.split("://", 1)
        if "@" in rest:
            return f"{scheme}://***@{rest.split('@', 1)[1]}"
    return dsn
