"""Federated-Learning FastAPI service.

Coordinates privacy-preserving model training across labs. Labs train locally on
private QC data and submit only model WEIGHT UPDATES. The server aggregates them
with FedAvg (weighted by sample count), clipping update L2 norms and adding
Gaussian noise for (epsilon, delta)-differential privacy. No raw QC data ever
leaves a lab.

Endpoints
---------
* ``POST /rounds/start``                 -> open a new round
* ``POST /rounds/{round}/submit``        -> submit a lab weight update
* ``POST /rounds/{round}/aggregate``     -> DP-FedAvg the round, advance model
* ``GET  /model/current``                -> current global model metadata
* ``GET  /health``                       -> liveness probe
* ``GET  /metrics``                      -> Prometheus metrics
"""

from __future__ import annotations

import math
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from loguru import logger
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel, Field

from .rounds import RoundManager

VERSION = "0.1.0"
SERVICE_NAME = "federated-learning"

# -- Prometheus metrics -------------------------------------------------------
# NOTE: deliberately no per-lab label on the updates counter -- lab_id is
# unbounded/high-cardinality and would blow up the metric series. Track the
# headcount of a round via a Gauge instead.
ROUNDS_TOTAL = Counter("qconnect_fl_rounds_total", "Total federated rounds started")
UPDATES_TOTAL = Counter("qconnect_fl_updates_total", "Total weight updates submitted")
PARTICIPATING_LABS = Gauge(
    "qconnect_fl_participating_labs", "Labs participating in the last aggregated round"
)
GLOBAL_MODEL_VERSION = Gauge("qconnect_fl_global_model_version", "Current global model version")
AGGREGATE_DURATION = Histogram(
    "qconnect_fl_aggregate_duration_seconds", "Time spent aggregating a round"
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialise the in-memory round manager on startup."""
    logger.info("starting {} v{}", SERVICE_NAME, VERSION)
    app.state.round_manager = RoundManager()
    yield
    logger.info("stopping {}", SERVICE_NAME)


app = FastAPI(title="QConnect-AI :: Federated Learning", version=VERSION, lifespan=lifespan)


def _manager() -> RoundManager:
    return app.state.round_manager


# -- Schemas ------------------------------------------------------------------
class WeightUpdate(BaseModel):
    """A single lab's model weight update for a round."""

    lab_id: str = Field(..., min_length=1)
    weights: list[float] = Field(..., min_length=1)
    num_samples: int = Field(..., gt=0)


class AggregateRequest(BaseModel):
    """DP-FedAvg aggregation parameters.

    ``epsilon`` may be ``null`` to disable noise (no privacy). ``clip_norm`` is
    the per-update L2 sensitivity bound.
    """

    epsilon: float | None = Field(default=1.0, gt=0)
    delta: float = Field(default=1e-5, gt=0, lt=1)
    clip_norm: float = Field(default=1.0, gt=0)


class StartRoundResponse(BaseModel):
    round_number: int


class SubmitResponse(BaseModel):
    accepted: bool
    total_updates: int


class AggregateResponse(BaseModel):
    round_number: int
    participating_labs: int
    global_model_version: int
    dp_epsilon: float | None
    dp_sigma: float
    global_model_dim: int
    global_model_norm: float
    # Truncated preview to keep payloads small for high-dimensional models.
    global_weights_preview: list[float]


class ModelStatus(BaseModel):
    global_model_version: int
    dim: int
    updated_at: str | None
    initialised: bool


# -- Endpoints ----------------------------------------------------------------
@app.post("/rounds/start", response_model=StartRoundResponse, tags=["federated"])
async def start_round() -> StartRoundResponse:
    """Open a new federated training round."""
    rn = _manager().start_round()
    ROUNDS_TOTAL.inc()
    logger.info("started round {}", rn)
    return StartRoundResponse(round_number=rn)


@app.post("/rounds/{round_number}/submit", response_model=SubmitResponse, tags=["federated"])
async def submit_update(round_number: int, update: WeightUpdate) -> SubmitResponse:
    """Submit a lab's weight update for ``round_number``."""
    try:
        total = _manager().submit_update(
            update.lab_id,
            {"weights": update.weights, "num_samples": update.num_samples},
            round_number,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    UPDATES_TOTAL.inc()
    logger.info(
        "round {} accepted update from lab {} ({} samples)",
        round_number,
        update.lab_id,
        update.num_samples,
    )
    return SubmitResponse(accepted=True, total_updates=total)


@app.post(
    "/rounds/{round_number}/aggregate",
    response_model=AggregateResponse,
    tags=["federated"],
)
async def aggregate_round(round_number: int, req: AggregateRequest) -> AggregateResponse:
    """Run DP-FedAvg over the round's updates and advance the global model."""
    start = time.perf_counter()
    # epsilon=None (or non-finite) means "no privacy" -> infinite epsilon, no noise.
    epsilon = math.inf if req.epsilon is None else req.epsilon
    try:
        result = _manager().aggregate_round(round_number, epsilon, req.delta, req.clip_norm)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        AGGREGATE_DURATION.observe(time.perf_counter() - start)

    weights = result["global_weights"]
    norm = math.sqrt(sum(w * w for w in weights))
    reported_epsilon = result["dp_epsilon"]
    if not math.isfinite(reported_epsilon):
        reported_epsilon = None
    PARTICIPATING_LABS.set(result["participating_labs"])
    GLOBAL_MODEL_VERSION.set(result["global_model_version"])
    logger.info(
        "aggregated round {}: {} labs, model v{}, sigma={:.4f}",
        round_number,
        result["participating_labs"],
        result["global_model_version"],
        result["dp_sigma"],
    )
    return AggregateResponse(
        round_number=result["round_number"],
        participating_labs=result["participating_labs"],
        global_model_version=result["global_model_version"],
        dp_epsilon=reported_epsilon,
        dp_sigma=result["dp_sigma"],
        global_model_dim=len(weights),
        global_model_norm=norm,
        global_weights_preview=weights[:16],
    )


@app.get("/model/current", response_model=ModelStatus, tags=["federated"])
async def model_current() -> ModelStatus:
    """Return metadata about the current global model."""
    mgr = _manager()
    weights = mgr.get_global_model()
    updated = mgr.updated_at
    return ModelStatus(
        global_model_version=mgr.model_version,
        dim=0 if weights is None else len(weights),
        updated_at=None if updated is None else updated.isoformat(),
        initialised=weights is not None,
    )


@app.get("/health", tags=["ops"])
async def health() -> dict:
    """Liveness probe."""
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "version": VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/metrics", tags=["ops"])
async def metrics() -> Response:
    """Prometheus metrics exposition."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8002)
