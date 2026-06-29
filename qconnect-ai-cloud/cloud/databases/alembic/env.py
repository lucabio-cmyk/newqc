"""Alembic migration environment for the QConnect-AI cloud database.

Design notes
------------
* The DB URL is taken from ``ALEMBIC_DATABASE_URL`` or ``DATABASE_URL`` (the same
  variable the app uses), falling back to the ``sqlalchemy.url`` in alembic.ini.
  An async ``postgresql+asyncpg://`` URL is normalised to a sync driver because
  Alembic runs synchronously.
* Migrations are authored as **raw SQL / explicit ``op`` calls** (no ORM model
  autogenerate), so ``target_metadata`` is ``None``. This keeps the migration
  environment free of any application import.
* Both offline (``alembic upgrade --sql``) and online modes are supported.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def _resolve_url() -> str:
    """Return a sync SQLAlchemy URL for Alembic."""
    url = (
        os.getenv("ALEMBIC_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or config.get_main_option("sqlalchemy.url")
        or ""
    )
    # Alembic is synchronous: swap any async driver for a sync one.
    url = url.replace("+asyncpg", "+psycopg").replace("+aiopg", "+psycopg")
    return url


def run_migrations_offline() -> None:
    """Emit SQL without a live DB connection (``--sql`` mode)."""
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _resolve_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
