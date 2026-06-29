"""Async repositories over the QConnect-AI ORM models.

Each repository wraps an :class:`~sqlalchemy.ext.asyncio.AsyncSession` and uses
SQLAlchemy 2.0 style (``select()`` + ``await session.execute()``). Write methods
``flush`` (assigning PKs / surfacing constraint errors) but never ``commit`` —
the calling endpoint owns the transaction boundary so a single request can
atomically write a QC result, its AI prediction and an auto-drafted CAPA.

All mapping is defensive (``.get()`` with sensible defaults) so partial payloads
from upstream (e.g. a degraded ML layer) never raise.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select

from cloud.db.models import AIPrediction, CAPAAction, QCResult


def _utcnow() -> datetime:
    """Timezone-aware UTC now."""
    return datetime.now(timezone.utc)


def _to_uuid(value: Any) -> UUID | None:
    """Coerce a value to a UUID (accepts UUID or str); None passes through."""
    if value is None or isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    """Best-effort float coercion that never raises."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class QCResultRepository:
    """Read/write access to ``qc_results``."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def create(self, qc: dict, response: dict) -> QCResult:
        """Map a QCDataInput dict + QCEvaluationResponse dict into a row.

        Args:
            qc: ``QCDataInput.model_dump()`` (analyzer/analyte/values...).
            response: ``QCEvaluationResponse.model_dump()`` with
                ``legacy_results`` (westgard/qconnect/sigma) and ``ai_insights``.

        Returns the flushed (PK-assigned) instance without committing.
        """
        legacy = response.get("legacy_results") or {}
        westgard = legacy.get("westgard") or {}
        qconnect = legacy.get("qconnect") or {}
        sigma = legacy.get("sigma") or {}
        ai = response.get("ai_insights") or {}

        # test_date: prefer the inbound analyzer timestamp, else now. The dict
        # may be JSON-serialized (ISO string) — coerce to a datetime so the
        # DateTime column accepts it on both SQLite and Postgres.
        test_date = (
            _parse_dt(qc.get("timestamp")) or _parse_dt(response.get("timestamp")) or _utcnow()
        )

        row = QCResult(
            run_id=uuid4(),
            test_date=test_date,
            analyzer_id=qc.get("analyzer_id"),
            analyte_code=qc.get("analyte_code"),
            qc_lot_id=qc.get("qc_lot_id"),
            result_value=_to_float(qc.get("result_value")),
            target_value=_to_float(qc.get("target_value")),
            sd_value=_to_float(qc.get("sd_value")),
            cv_percent=_to_float(westgard.get("cv_percent")),
            bias_percent=_to_float(westgard.get("bias_percent")),
            deviation_sd=_to_float(westgard.get("deviation_sd")),
            westgard_rule_violated=westgard.get("rule_violated"),
            westgard_status=westgard.get("status"),
            qconnect_status=qconnect.get("status"),
            sigma_status=sigma.get("advisory_status") or sigma.get("status"),
            diagnostic_sigma=_to_float(ai.get("diagnostic_sigma")),
            ai_anomaly_score=_to_float(ai.get("anomaly_score")),
            ai_failure_probability_48h=_to_float(ai.get("failure_probability_48h")),
            qc_status=response.get("qc_status"),
            severity=response.get("severity"),
            operator_id=qc.get("operator_id"),
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def recent_history(
        self,
        analyte_code: str,
        qc_lot_id: str | None = None,
        limit: int = 30,
    ) -> list[float]:
        """Return recent ``result_value``s for an analyte, oldest-first.

        Filters by ``analyte_code`` (and ``qc_lot_id`` when supplied), orders by
        ``test_date`` DESC, takes ``limit`` rows, then reverses so the returned
        list is chronological (oldest -> newest) as the engines expect.
        """
        stmt = (
            select(QCResult.result_value)
            .where(
                QCResult.analyte_code == analyte_code,
                QCResult.result_value.is_not(None),
            )
            .order_by(QCResult.test_date.desc())
            .limit(limit)
        )
        if qc_lot_id is not None:
            stmt = stmt.where(QCResult.qc_lot_id == qc_lot_id)
        result = await self._session.execute(stmt)
        values = [float(v) for v in result.scalars().all() if v is not None]
        values.reverse()  # DESC fetch -> oldest-first for the engines.
        return values

    async def lab_status_counts(self, lab_id: str, window_hours: int = 24) -> dict:
        """Aggregate qc_status counts within a recency window.

        LIMITATION: ``qc_results`` has no ``lab_id`` column (see
        :class:`cloud.db.models.QCResult`), so the ``lab_id`` argument cannot be
        used to filter rows. We therefore aggregate ALL results within the
        recency window. The returned dict still carries ``lab_id`` so the
        endpoint can echo it back. This is a documented, non-crashing
        degradation; against a future schema with lab scoping the WHERE clause
        would simply gain a ``lab_id`` predicate.
        """
        # Window on created_at (when the row was persisted / evaluated) rather
        # than test_date, which is the analyzer-supplied measurement time and may
        # be historical (back-filled batches), so "recent activity" reflects
        # ingestion recency. Same column works on SQLite and Postgres.
        since = _utcnow() - timedelta(hours=window_hours)
        stmt = (
            select(QCResult.qc_status, func.count())
            .where(QCResult.created_at >= since)
            .group_by(QCResult.qc_status)
        )
        result = await self._session.execute(stmt)
        status_counts: dict[str, int] = {}
        total = 0
        for qc_status, count in result.all():
            key = qc_status or "UNKNOWN"
            status_counts[key] = int(count)
            total += int(count)

        last_stmt = select(func.max(QCResult.created_at)).where(QCResult.created_at >= since)
        last_evaluated_at = (await self._session.execute(last_stmt)).scalar_one_or_none()

        return {
            "lab_id": lab_id,
            "window_hours": window_hours,
            "total_results": total,
            "status_counts": status_counts,
            "pass_count": status_counts.get("PASS", 0),
            "fail_count": status_counts.get("FAIL", 0),
            "review_required_count": status_counts.get("REVIEW_REQUIRED", 0),
            "hold_pending_ai_count": status_counts.get("HOLD_PENDING_AI", 0),
            "last_evaluated_at": last_evaluated_at,
        }


class CAPARepository:
    """Write access to ``capa_actions``."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def create(self, capa: dict, qc_result_id: UUID | str | None = None) -> CAPAAction:
        """Map a drafted-CAPA dict (from rca_capa) into a row and flush it."""
        row = CAPAAction(
            capa_number=capa.get("capa_number") or f"CAPA-{uuid4().hex[:8].upper()}",
            qc_result_id=_to_uuid(qc_result_id),
            incident_date=_parse_dt(capa.get("incident_date")),
            severity=capa.get("severity"),
            root_cause_category=capa.get("root_cause_category"),
            root_cause_description=capa.get("root_cause_description"),
            ai_suggested_causes=capa.get("ai_suggested_causes"),
            ai_rca_confidence=_to_float(capa.get("ai_rca_confidence")),
            immediate_action=capa.get("immediate_action"),
            preventive_action=capa.get("preventive_action"),
            target_closure_date=_parse_dt(capa.get("target_closure_date")),
            monitoring_period_days=capa.get("monitoring_period_days"),
            status=capa.get("status", "OPEN"),
        )
        self._session.add(row)
        await self._session.flush()
        return row


class AIPredictionRepository:
    """Write access to ``ai_predictions``."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def create(self, qc_result_id: UUID | str | None, ai: dict) -> AIPrediction:
        """Map AI insights into an ``ai_predictions`` row and flush it."""
        fp48 = _to_float(ai.get("failure_probability_48h"))
        row = AIPrediction(
            qc_result_id=_to_uuid(qc_result_id),
            failure_probability_48h=fp48,
            failure_risk_level=_risk_level(fp48),
            anomaly_detected=bool(ai.get("anomaly_detected", False)),
            anomaly_score=_to_float(ai.get("anomaly_score")),
            model_version=ai.get("model_version"),
            model_confidence=_to_float(ai.get("model_confidence")),
        )
        self._session.add(row)
        await self._session.flush()
        return row


def _risk_level(fp48: float | None) -> str | None:
    """Bucket a 48h failure probability into a coarse risk level."""
    if fp48 is None:
        return None
    if fp48 >= 0.8:
        return "HIGH"
    if fp48 >= 0.5:
        return "MEDIUM"
    return "LOW"


def _parse_dt(value: Any) -> datetime | None:
    """Parse an ISO date/datetime string (or pass through a datetime)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None
