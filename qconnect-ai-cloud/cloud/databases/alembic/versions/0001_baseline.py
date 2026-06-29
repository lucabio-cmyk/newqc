"""Baseline schema (applies the canonical init.sql).

This is the single source of truth for the initial schema: rather than
duplicating the DDL as ``op.create_table`` calls, the baseline executes
``cloud/databases/postgres/init.sql`` verbatim. That file is fully idempotent
(``CREATE TABLE/INDEX/EXTENSION IF NOT EXISTS`` and guarded trigger creation),
so applying the baseline to a fresh database is safe.

For a database that was already initialised directly from init.sql, stamp this
revision without re-running it::

    alembic stamp 0001_baseline

Revision ID: 0001_baseline
Revises:
Create Date: 2026-06-29
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# .../databases/alembic/versions/0001_baseline.py -> .../databases/postgres/init.sql
_INIT_SQL = Path(__file__).resolve().parents[2] / "postgres" / "init.sql"

# Tables created by init.sql, in dependency order (children last) so downgrade
# can drop them children-first.
_TABLES = [
    "qc_results",
    "control_limits",
    "control_limits_history",
    "qc_materials",
    "westgard_sigma_metrics",
    "diagnostic_sigma_outcomes",
    "capa_actions",
    "distribution_predictions",
    "ai_predictions",
    "federated_learning_updates",
    "audit_trail",
]


def upgrade() -> None:
    """Apply the canonical schema by executing init.sql."""
    sql = _INIT_SQL.read_text(encoding="utf-8")
    op.execute(sql)


def downgrade() -> None:
    """Drop everything the baseline created (children first via CASCADE)."""
    for table in reversed(_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at() CASCADE")
