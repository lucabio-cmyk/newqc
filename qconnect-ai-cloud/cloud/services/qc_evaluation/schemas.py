"""Self-contained Pydantic v2 schemas for the QC-Evaluation service.

These models intentionally **re-declare** the canonical wire contract that also
lives in the sibling package ``qconnect-ai-shared`` (``shared.models``). They are
duplicated here so that the cloud project can be built as a standalone Docker
image without depending on the shared package being installed in the build
context. The two definitions MUST stay byte-for-byte compatible at the JSON
level — an evaluation request serialized at the edge has to deserialize
identically here.

Compatibility note
------------------
Written with Python 3.12 syntax (``from __future__ import annotations``, ``X |
None``, ``list[str]``) but importable on Python 3.11. No PEP 695 ``type``
aliases and no new generic syntax are used.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _utcnow() -> datetime:
    """Return a timezone-aware UTC ``datetime``.

    ``datetime.utcnow()`` is deprecated from Python 3.12 onward because it
    returns a naive value; we always carry tz info so cross-service comparisons
    never raise.
    """
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Enums (canonical wire vocabulary)
# --------------------------------------------------------------------------- #
class AnalyteType(str, Enum):
    """Broad analytical discipline of an analyte."""

    SEROLOGY = "serology"
    CHEMISTRY = "chemistry"
    HEMATOLOGY = "hematology"
    COAGULATION = "coagulation"
    NAT = "nat"  # nucleic acid testing


class QCLevelType(str, Enum):
    """Control material level."""

    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    CUSTOM = "CUSTOM"


class QCStatusEnum(str, Enum):
    """Outcome of a QC evaluation."""

    PASS = "PASS"
    FAIL = "FAIL"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    HOLD_PENDING_AI = "HOLD_PENDING_AI"


class SeverityEnum(str, Enum):
    """Clinical / operational severity of a QC event."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


# --------------------------------------------------------------------------- #
# Inbound: a single QC result from an analyzer
# --------------------------------------------------------------------------- #
class QCDataInput(BaseModel):
    """A single QC result emitted by an analyzer and submitted for evaluation."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "lab_id": "lab-genova-001",
                "analyzer_id": "ABBOTT-ARCHITECT-001",
                "analyte_code": "HCV-AB",
                "analyte_type": "serology",
                "qc_lot_id": "QC-HCV-DIAMEX-202603-001",
                "qc_level": "NORMAL",
                "result_value": 1.45,
                "target_value": 1.50,
                "sd_value": 0.08,
                "operator_id": "EMP00234",
                "correlation_id": "9f1c2b3d4e5f6a7b8c9d0e1f2a3b4c5d",
                "timestamp": "2026-03-15T09:30:00Z",
            }
        }
    )

    lab_id: str = Field(..., min_length=1, description="Originating laboratory id")
    analyzer_id: str = Field(..., min_length=1, description="Instrument identifier")
    analyte_code: str = Field(..., min_length=1, description="Analyte code (LOINC or local)")
    analyte_type: AnalyteType
    qc_lot_id: str = Field(..., min_length=1, description="QC material lot identifier")
    qc_level: QCLevelType
    result_value: float = Field(..., gt=0, description="Measured QC value")
    target_value: float = Field(..., description="Assigned target/mean for the lot")
    sd_value: float = Field(..., gt=0, description="Assigned standard deviation for the lot")
    operator_id: str = Field(..., min_length=1, description="Operator who ran the QC")
    correlation_id: str | None = Field(
        default=None, description="Trace id propagated across services"
    )
    timestamp: datetime = Field(default_factory=_utcnow)

    @field_validator("timestamp")
    @classmethod
    def _ensure_tz(cls, v: datetime) -> datetime:
        """Coerce naive datetimes to UTC so comparisons never raise."""
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v


# --------------------------------------------------------------------------- #
# Per-engine result fragments
# --------------------------------------------------------------------------- #
class WestgardResult(BaseModel):
    """Outcome of the Westgard multirule engine."""

    status: QCStatusEnum
    rule_violated: str | None = Field(default=None, description="e.g. '1-3S', '2-2S'")
    rules_checked: list[str] = Field(default_factory=list)
    mean: float
    sd: float
    cv_percent: float
    deviation_sd: float = Field(..., description="(value - mean) / sd, signed")


class QConnectResult(BaseModel):
    """Outcome of the QConnect non-Gaussian limits engine."""

    status: QCStatusEnum
    percentile_5: float
    percentile_95: float
    percentile_position: float = Field(
        ..., ge=0, le=1, description="Empirical CDF position of the value"
    )
    lcl: float = Field(..., description="Lower control limit")
    ucl: float = Field(..., description="Upper control limit")


class SigmaResult(BaseModel):
    """Six Sigma quality metric for the method."""

    sigma_metric: float
    sigma_category: str = Field(..., description="'>6', '4-6', '3-4', '2-3', '<2'")
    recommended_rules: str
    expected_frr_percent: float = Field(..., description="Expected false rejection rate (percent)")


class AIInsights(BaseModel):
    """AI-derived enrichment layered on top of the legacy engines."""

    distribution_type: str = Field(default="unknown")
    failure_probability_48h: float | None = Field(default=None, ge=0, le=1)
    anomaly_detected: bool = False
    anomaly_score: float | None = Field(default=None)
    diagnostic_sigma: float | None = None
    clinical_impact_percent: float | None = None
    recommended_action: str | None = None


# --------------------------------------------------------------------------- #
# Outbound: unified evaluation response
# --------------------------------------------------------------------------- #
class QCEvaluationResponse(BaseModel):
    """Unified decision returned to the caller after fusing all engines."""

    qc_status: QCStatusEnum
    severity: SeverityEnum
    legacy_results: dict[str, dict] = Field(
        default_factory=dict,
        description="Per-engine raw fragments. Keys: 'westgard', 'qconnect', 'sigma'",
    )
    ai_insights: AIInsights
    recommendation: str | None = None
    confidence: float = Field(..., ge=0, le=1)
    correlation_id: str | None = None
    evaluated_offline: bool = Field(
        default=False,
        description="True when produced by an edge node without cloud connectivity",
    )
    timestamp: datetime = Field(default_factory=_utcnow)


# --------------------------------------------------------------------------- #
# CAPA
# --------------------------------------------------------------------------- #
class CAPAActionInput(BaseModel):
    """Corrective And Preventive Action opened against a failed QC result."""

    lab_id: str = Field(..., min_length=1)
    qc_result_id: str = Field(..., min_length=1, description="UUID of the offending qc_results row")
    severity: SeverityEnum
    root_cause_category: str = Field(..., min_length=1)
    root_cause_description: str = Field(..., min_length=1)
    immediate_action: str = Field(..., min_length=1)
    preventive_action: str | None = None


# --------------------------------------------------------------------------- #
# Batch upload (edge -> cloud)
# --------------------------------------------------------------------------- #
class QCBatchUpload(BaseModel):
    """Envelope for a batch of QC results synced from an edge node."""

    lab_id: str = Field(..., min_length=1)
    records: list[QCDataInput] = Field(..., min_length=1, max_length=1000)
    sent_at: datetime = Field(default_factory=_utcnow)


class QCBatchAccepted(BaseModel):
    """Acknowledgement returned for a batch upload."""

    lab_id: str
    accepted: int = Field(..., ge=0, description="Number of records accepted/stored")
    rejected: int = Field(default=0, ge=0, description="Number of records rejected")
    correlation_id: str | None = None
    received_at: datetime = Field(default_factory=_utcnow)


# --------------------------------------------------------------------------- #
# Lab QC status summary (placeholder/stub payload)
# --------------------------------------------------------------------------- #
class LabQCStatusSummary(BaseModel):
    """Recent-status summary for a single lab.

    Placeholder shape: in production these counts are aggregated from the
    ``qc_results`` table over a rolling window.
    """

    lab_id: str
    window_hours: int = 24
    total_results: int = 0
    pass_count: int = 0
    fail_count: int = 0
    review_required_count: int = 0
    hold_pending_ai_count: int = 0
    last_evaluated_at: datetime | None = None
    generated_at: datetime = Field(default_factory=_utcnow)


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
class HealthStatus(BaseModel):
    """Standard health probe payload."""

    status: str = "ok"
    service: str
    version: str
    checks: dict[str, bool] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=_utcnow)


# --------------------------------------------------------------------------- #
# Structured error envelope
# --------------------------------------------------------------------------- #
class ErrorResponse(BaseModel):
    """Uniform JSON error body returned by exception handlers."""

    error: str = Field(..., description="Short machine-readable error code")
    message: str = Field(..., description="Human-readable explanation")
    correlation_id: str | None = None
