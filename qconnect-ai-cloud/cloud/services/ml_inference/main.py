"""ML-Inference FastAPI service.

Serves AI enrichment for QC evaluation: a 48h failure probability, an anomaly
score, a clinical-impact estimate and a recommended action. The numbers come
from transparent, numpy-based heuristics (see the ``models`` package) — NOT a
trained model — but the endpoint is real, explainable and stable so the
``qc_evaluation`` orchestrator integrates against a production-shaped contract.

Wire contract (consumed by qc_evaluation):
    POST /predict <- full QCDataInput JSON (+ optional ``history: list[float]``)
    -> JSON including: failure_probability_48h, anomaly_detected, anomaly_score,
       clinical_impact_percent, recommended_action (plus extra context fields).

The request model is lenient (``extra="allow"``) so new caller fields never 422.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Response
from loguru import logger
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel, ConfigDict, Field

from models import AnomalyDetector, Explainer, FailurePredictor

VERSION = "0.2.0"
SERVICE_NAME = "ml-inference"

# --------------------------------------------------------------------------- #
# Prometheus metrics
# --------------------------------------------------------------------------- #
PREDICTIONS_TOTAL = Counter(
    "qconnect_ml_predictions_total",
    "Total /predict calls served, labelled by predicted failure risk level.",
    ["risk_level"],
)
PREDICT_DURATION = Histogram(
    "qconnect_ml_predict_duration_seconds",
    "Wall-clock duration of /predict handling in seconds.",
)
LAST_FAILURE_PROBABILITY = Gauge(
    "qconnect_ml_last_failure_probability",
    "Failure probability (48h) returned by the most recent /predict call.",
)

# --------------------------------------------------------------------------- #
# Model singletons (stateless heuristics)
# --------------------------------------------------------------------------- #
_failure_predictor = FailurePredictor()
_anomaly_detector = AnomalyDetector()
_explainer = Explainer()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: log startup/shutdown and warm the models."""
    logger.info("{} v{} starting up", SERVICE_NAME, VERSION)
    # Touch the models once so any import-time issues surface at startup.
    _failure_predictor.predict(1.0, 1.0, 1.0, [])
    yield
    logger.info("{} v{} shutting down", SERVICE_NAME, VERSION)


app = FastAPI(title="QConnect-AI :: ML Inference", version=VERSION, lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class PredictRequest(BaseModel):
    """Lenient mirror of QCDataInput (plus an optional ``history``).

    ``extra="allow"`` means the full QCDataInput payload — and any future caller
    fields — can be posted verbatim without triggering a 422.
    """

    model_config = ConfigDict(extra="allow")

    analyte_code: str = "UNKNOWN"
    analyte_type: str = "chemistry"
    result_value: float = 0.0
    target_value: float = 0.0
    sd_value: float = 1.0
    history: list[float] | None = None


class ContributingFactor(BaseModel):
    """One ranked feature contribution to the failure-probability logit."""

    name: str
    value: float
    weight: float
    contribution: float


class PredictResponse(BaseModel):
    """AIInsights-compatible prediction payload (a superset of the contract)."""

    # Required by the qc_evaluation orchestrator.
    failure_probability_48h: float = Field(ge=0, le=1)
    anomaly_detected: bool
    anomaly_score: float
    clinical_impact_percent: float
    recommended_action: str

    # Additional explainability / context.
    distribution_type: str = "unknown"
    failure_risk_level: str = "low"
    timeline_hours: float = 48.0
    contributing_factors: list[ContributingFactor] = Field(default_factory=list)
    model_version: str = VERSION
    model_confidence: float = 0.0


def _model_confidence(history: list[float]) -> float:
    """Heuristic confidence in ``[0, 1]`` that scales with available history.

    More history -> more reliable robust statistics -> higher confidence. A
    saturating curve caps confidence at 0.95 (these are heuristics, never 1.0).
    """
    n = len(history)
    if n == 0:
        return 0.3
    return round(min(0.95, 0.3 + 0.65 * (n / (n + 10.0)) * 2.0), 4)


def _predict(req: PredictRequest) -> PredictResponse:
    """Run the heuristic pipeline: failure predictor -> anomaly -> explainer."""
    history = [float(x) for x in (req.history or [])]

    prediction = _failure_predictor.predict(
        req.result_value, req.target_value, req.sd_value, history
    )
    anomaly = _anomaly_detector.score(req.result_value, history)
    explanation = _explainer.explain(prediction, anomaly, req.analyte_type)

    return PredictResponse(
        failure_probability_48h=prediction["failure_probability_48h"],
        anomaly_detected=anomaly["anomaly_detected"],
        anomaly_score=anomaly["anomaly_score"],
        clinical_impact_percent=explanation["clinical_impact_percent"],
        recommended_action=explanation["recommended_action"],
        failure_risk_level=prediction["failure_risk_level"],
        timeline_hours=prediction["timeline_hours"],
        contributing_factors=[ContributingFactor(**f) for f in prediction["contributing_factors"]],
        model_confidence=_model_confidence(history),
    )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.post("/predict", response_model=PredictResponse, tags=["inference"])
async def predict(req: PredictRequest) -> PredictResponse:
    """Return AI enrichment for a single QC point (heuristic, explainable)."""
    start = time.perf_counter()
    response = _predict(req)
    PREDICT_DURATION.observe(time.perf_counter() - start)
    PREDICTIONS_TOTAL.labels(risk_level=response.failure_risk_level).inc()
    LAST_FAILURE_PROBABILITY.set(response.failure_probability_48h)
    logger.debug(
        "predict analyte={} risk={} p48={:.3f} anomaly={}",
        req.analyte_code,
        response.failure_risk_level,
        response.failure_probability_48h,
        response.anomaly_detected,
    )
    return response


@app.get("/health", tags=["ops"])
async def health() -> dict[str, Any]:
    """Liveness/readiness probe with component checks."""
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "version": VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": {
            "failure_predictor": "ok",
            "anomaly_detector": "ok",
            "explainer": "ok",
        },
    }


@app.get("/metrics", tags=["ops"])
async def metrics() -> Response:
    """Prometheus metrics exposition."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)
