"""ML-Inference FastAPI service (placeholder).

Exposes ``POST /predict`` returning an AIInsights-shaped payload so the
qc_evaluation orchestrator has a real endpoint to call. The numbers are derived
from cheap heuristics over the incoming QC point — NOT a trained model. Replace
``_predict`` with real model inference (joblib/onnx) in production.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from pydantic import BaseModel, Field

VERSION = "0.1.0"
SERVICE_NAME = "ml-inference"

app = FastAPI(title="QConnect-AI :: ML Inference", version=VERSION)


class PredictRequest(BaseModel):
    """Subset of QCDataInput fields the predictor consumes.

    Extra fields are ignored so the full QCDataInput payload can be posted
    verbatim by qc_evaluation.
    """

    model_config = {"extra": "ignore"}

    analyte_code: str = "UNKNOWN"
    result_value: float = 0.0
    target_value: float = 0.0
    sd_value: float = 1.0
    analyte_type: str = "chemistry"


class PredictResponse(BaseModel):
    """AIInsights-compatible prediction payload."""

    distribution_type: str = "unknown"
    failure_probability_48h: float = Field(ge=0, le=1)
    anomaly_detected: bool
    anomaly_score: float
    clinical_impact_percent: float | None = None
    recommended_action: str | None = None
    model_version: str = VERSION


def _predict(req: PredictRequest) -> PredictResponse:
    """Heuristic stand-in for a trained model.

    Uses the z-score of the point to fabricate a monotonic failure probability
    and anomaly score. This keeps the integration honest (bigger deviation ->
    higher risk) without pretending to be a real model.
    """
    sd = req.sd_value or 1.0
    z = abs(req.result_value - req.target_value) / sd if sd else 0.0
    # Squash z into [0,1] with a logistic-ish curve.
    failure_prob = min(1.0, round(1.0 - 1.0 / (1.0 + 0.35 * z**2), 4))
    anomaly_score = round(min(1.0, z / 4.0), 4)
    anomaly = z >= 2.5
    action = None
    if failure_prob >= 0.8:
        action = "Schedule preventive maintenance / recalibration within 24h."
    elif anomaly:
        action = "Investigate possible reagent or calibration drift."
    return PredictResponse(
        distribution_type="unknown",
        failure_probability_48h=failure_prob,
        anomaly_detected=anomaly,
        anomaly_score=anomaly_score,
        clinical_impact_percent=round(min(100.0, z * 5.0), 2),
        recommended_action=action,
    )


@app.post("/predict", response_model=PredictResponse, tags=["inference"])
async def predict(req: PredictRequest) -> PredictResponse:
    """Return AI enrichment for a single QC point (placeholder heuristics)."""
    return _predict(req)


@app.get("/health", tags=["ops"])
async def health() -> dict:
    """Liveness probe."""
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "version": VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)
