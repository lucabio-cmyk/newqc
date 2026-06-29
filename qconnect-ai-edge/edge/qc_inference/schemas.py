"""Self-contained wire-contract schemas for the QConnect-AI EDGE node.

These Pydantic v2 models intentionally *re-declare* the canonical contract that
lives in the sibling package ``qconnect-ai-shared`` (``shared.models``). Keeping a
local copy lets the edge image be built and deployed in Docker without depending
on the shared wheel, which keeps the edge node small, self-contained and able to
boot in an air-gapped lab.

The definitions here MUST stay byte-for-byte compatible with ``shared.models``;
they form the JSON wire contract spoken between the edge and the cloud. If the
canonical contract changes, mirror the change here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _utcnow() -> datetime:
    """Return a timezone-aware UTC ``datetime`` (``utcnow`` is deprecated)."""
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Enums (mirror shared.models)
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
    """Clinical/operational severity of a QC event."""

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
    operator_id: str = Field(..., min_length=1)
    correlation_id: Optional[str] = Field(
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
# AI enrichment fragment
# --------------------------------------------------------------------------- #
class AIInsights(BaseModel):
    """AI-derived enrichment layered on top of the legacy engines."""

    distribution_type: str = Field(default="unknown")
    failure_probability_48h: Optional[float] = Field(default=None, ge=0, le=1)
    anomaly_detected: bool = False
    anomaly_score: Optional[float] = Field(default=None)
    diagnostic_sigma: Optional[float] = None
    clinical_impact_percent: Optional[float] = None
    recommended_action: Optional[str] = None


# --------------------------------------------------------------------------- #
# Outbound: unified evaluation response
# --------------------------------------------------------------------------- #
class QCEvaluationResponse(BaseModel):
    """Unified decision returned to the caller after fusing all engines."""

    qc_status: QCStatusEnum
    severity: SeverityEnum
    legacy_results: dict[str, dict] = Field(
        default_factory=dict, description="Keys: 'westgard', 'qconnect', 'sigma'"
    )
    ai_insights: AIInsights
    recommendation: Optional[str] = None
    confidence: float = Field(..., ge=0, le=1)
    correlation_id: Optional[str] = None
    evaluated_offline: bool = Field(
        default=False, description="True when produced by an edge node without cloud"
    )
    timestamp: datetime = Field(default_factory=_utcnow)


# --------------------------------------------------------------------------- #
# Batch upload envelope (edge -> cloud)
# --------------------------------------------------------------------------- #
class QCBatchUpload(BaseModel):
    """Envelope for a batch of QC results synced from an edge node."""

    lab_id: str = Field(..., min_length=1)
    records: list[QCDataInput] = Field(..., min_length=1, max_length=1000)
    sent_at: datetime = Field(default_factory=_utcnow)


class HealthStatus(BaseModel):
    """Standard health probe payload."""

    status: str = "ok"
    service: str
    version: str
    checks: dict[str, bool] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=_utcnow)
