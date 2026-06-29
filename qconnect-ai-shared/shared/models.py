"""Shared Pydantic v2 models and enums for QConnect-AI.

These define the wire contract between the edge and the cloud. They are
intentionally free of any persistence or framework concern so they can be
imported from FastAPI services, sync daemons and test suites alike.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _utcnow() -> datetime:
    """Timezone-aware UTC now (``datetime.utcnow`` is deprecated in 3.12)."""
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Enums
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
# Per-engine result fragments
# --------------------------------------------------------------------------- #
class WestgardResult(BaseModel):
    """Outcome of the Westgard multirule engine."""

    status: QCStatusEnum
    rule_violated: Optional[str] = Field(default=None, description="e.g. '1-3S', '2-2S'")
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
    percentile_position: float = Field(..., ge=0, le=1, description="Empirical CDF position")
    lcl: float = Field(..., description="Lower control limit")
    ucl: float = Field(..., description="Upper control limit")


class SigmaResult(BaseModel):
    """Six Sigma quality metric for the method."""

    sigma_metric: float
    sigma_category: str = Field(..., description="'>6', '4-6', '3-4', '2-3', '<2'")
    recommended_rules: str
    expected_frr_percent: float = Field(..., description="Expected false rejection rate")


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
# CAPA
# --------------------------------------------------------------------------- #
class CAPAActionInput(BaseModel):
    """Corrective And Preventive Action opened against a failed QC result."""

    lab_id: str = Field(..., min_length=1)
    qc_result_id: int = Field(..., ge=0)
    severity: SeverityEnum
    root_cause_category: str = Field(..., min_length=1)
    root_cause_description: str = Field(..., min_length=1)
    immediate_action: str = Field(..., min_length=1)
    preventive_action: Optional[str] = None


# --------------------------------------------------------------------------- #
# Batch upload (edge -> cloud)
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
