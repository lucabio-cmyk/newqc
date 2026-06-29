"""SQLAlchemy ORM models for the QConnect-AI cloud backend.

These models define a **portable subset** of the canonical Postgres schema in
``cloud/databases/postgres/init.sql`` — only the columns the application reads or
writes. The remaining init.sql columns are nullable or carry a DB-side default,
so omitting them here is safe against the real Postgres schema, while
``Base.metadata.create_all`` against SQLite (tests) simply builds the subset.

Portability rules honoured here so the same models work on both SQLite and
Postgres:

* Only portable column types are used (``sa.Uuid``, ``sa.Float``, ``sa.Text``,
  ``sa.Boolean``, ``sa.DateTime(timezone=True)``, ``sa.JSON``, ``sa.Integer``).
* No Postgres-only types (no ``JSONB``/``UUID``-with-``gen_random_uuid()``).
* No ``server_default`` calling ``now()`` / ``gen_random_uuid()``; every default
  is computed **Python-side** so both engines populate it identically.
* Every ``NOT NULL`` column from init.sql that lacks a DB default is given a
  Python default here (or is always set by the repository).
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from cloud.config.database import Base

__all__ = ["Base", "QCResult", "CAPAAction", "AIPrediction"]


def _utcnow() -> datetime:
    """Return a timezone-aware UTC ``datetime`` for Python-side defaults."""
    return datetime.now(timezone.utc)


class QCResult(Base):
    """A single evaluated QC measurement (``qc_results`` table).

    NOTE on lab filtering: the canonical ``qc_results`` table has **no
    ``lab_id`` column** (lab scoping in the schema is implied via analyzer /
    material lots, not stored on the row). We therefore do NOT invent one — see
    :meth:`QCResultRepository.lab_status_counts` for how lab-status aggregation
    degrades to a recency-only window.
    """

    __tablename__ = "qc_results"

    id: Mapped[UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid4)
    # NOT NULL + UNIQUE in init.sql — idempotency key; Python-side default.
    run_id: Mapped[UUID] = mapped_column(sa.Uuid, unique=True, nullable=False, default=uuid4)
    # NOT NULL in init.sql — Python-side default.
    test_date: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    analyzer_id: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    analyte_code: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    qc_lot_id: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    result_value: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    target_value: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    sd_value: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    cv_percent: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    bias_percent: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    deviation_sd: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    westgard_rule_violated: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    westgard_status: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    qconnect_status: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    sigma_status: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    diagnostic_sigma: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    ai_anomaly_score: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    ai_failure_probability_48h: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    qc_status: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    severity: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    operator_id: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )


class CAPAAction(Base):
    """A Corrective And Preventive Action record (``capa_actions`` table)."""

    __tablename__ = "capa_actions"

    id: Mapped[UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid4)
    # NOT NULL + UNIQUE in init.sql — always supplied by rca_capa draft.
    capa_number: Mapped[str] = mapped_column(sa.Text, unique=True, nullable=False)
    # init.sql: ON DELETE SET NULL — FK is nullable.
    qc_result_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("qc_results.id", ondelete="SET NULL"),
        nullable=True,
    )
    incident_date: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    severity: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    root_cause_category: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    root_cause_description: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    ai_suggested_causes: Mapped[object | None] = mapped_column(sa.JSON, nullable=True)
    ai_rca_confidence: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    immediate_action: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    preventive_action: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    target_closure_date: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    monitoring_period_days: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    status: Mapped[str | None] = mapped_column(sa.Text, nullable=True, default="OPEN")
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )


class AIPrediction(Base):
    """An AI/ML enrichment record tied to a QC result (``ai_predictions``)."""

    __tablename__ = "ai_predictions"

    id: Mapped[UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid4)
    # init.sql: ON DELETE CASCADE.
    qc_result_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("qc_results.id", ondelete="CASCADE"),
        nullable=True,
    )
    prediction_timestamp: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    failure_probability_48h: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    failure_risk_level: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    anomaly_detected: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    anomaly_score: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    model_version: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    model_confidence: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
