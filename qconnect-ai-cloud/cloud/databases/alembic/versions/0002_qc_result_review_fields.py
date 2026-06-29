"""Add reviewer audit fields to qc_results.

Illustrative incremental migration showing the intended forward-workflow:
small, reversible ``op`` changes layered on top of the baseline. Adds who/when
a flagged QC result was manually reviewed, plus an index to query a reviewer's
outstanding (unreviewed) queue.

Revision ID: 0002_qc_review_fields
Revises: 0001_baseline
Create Date: 2026-06-29
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_qc_review_fields"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("qc_results", sa.Column("reviewed_by", sa.Text(), nullable=True))
    op.add_column(
        "qc_results",
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Partial index over the manual-review backlog (status REVIEW_REQUIRED and
    # not yet reviewed) — keeps the reviewer-queue query cheap.
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_qc_results_review_queue
            ON qc_results (test_date)
            WHERE qc_status = 'REVIEW_REQUIRED' AND reviewed_at IS NULL
        """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_qc_results_review_queue")
    op.drop_column("qc_results", "reviewed_at")
    op.drop_column("qc_results", "reviewed_by")
