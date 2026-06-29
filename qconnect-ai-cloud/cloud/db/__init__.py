"""Persistence layer for the QConnect-AI cloud backend.

Exposes the SQLAlchemy ORM models (a portable subset of the canonical
``init.sql`` schema) and the async repository classes used by the services. The
ORM ``Base`` is the one declared in :mod:`cloud.config.database` so that all
models share a single metadata registry.
"""

from __future__ import annotations

from cloud.config.database import Base

__all__ = ["Base"]
