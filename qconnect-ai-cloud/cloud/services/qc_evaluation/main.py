"""QC-Evaluation FastAPI service.

This is the orchestration core of QConnect-AI. It accepts a single QC result (or
a batch synced from an edge node), runs the deterministic legacy engines
(Westgard multirule, QConnect non-Gaussian limits, Six Sigma, distribution
detection), enriches the verdict with the AI/ML inference service (best-effort,
degrading gracefully when unreachable) and fuses everything into one unified
:class:`schemas.QCEvaluationResponse`.

Fusion policy
-------------
* Any engine returning ``FAIL`` -> overall ``FAIL``.
* Engine discordance or a warning (1-2S / percentile edge) -> ``REVIEW_REQUIRED``.
* Otherwise -> ``PASS``.
Severity is mapped from the failing engine and analyte type.

Run (container):
    ``uvicorn main:app --host 0.0.0.0 --port 8000``

Import resilience
-----------------
The module supports two layouts:

* **Container** — the service directory is the working dir, so ``schemas``,
  ``dependencies`` and ``models`` import as top-level modules.
* **Monorepo / tests** — imports fall back to the fully-qualified
  ``cloud.services.qc_evaluation.*`` package path.
"""

from __future__ import annotations

import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from loguru import logger
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# --------------------------------------------------------------------------- #
# Import resilience: make sibling modules importable in either layout.
# --------------------------------------------------------------------------- #
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))
# Repo root (…/qconnect-ai-cloud) so that ``cloud.*`` imports resolve.
_REPO_ROOT = _THIS_DIR.parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import httpx  # noqa: E402
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import JSONResponse, Response  # noqa: E402

try:  # Container-style imports (service dir on path).
    import schemas  # type: ignore
    from dependencies import get_app_settings, get_current_principal, get_db  # type: ignore
    from models import (  # type: ignore
        DistributionDetector,
        QConnectEngine,
        SigmaEngine,
        WestgardEngine,
    )
except Exception:  # pragma: no cover - monorepo/test layout
    from cloud.services.qc_evaluation import schemas  # type: ignore
    from cloud.services.qc_evaluation.dependencies import (  # type: ignore
        get_app_settings,
        get_current_principal,
        get_db,
    )
    from cloud.services.qc_evaluation.models import (  # type: ignore
        DistributionDetector,
        QConnectEngine,
        SigmaEngine,
        WestgardEngine,
    )

try:
    from cloud.db.repositories import (  # type: ignore
        AIPredictionRepository,
        CAPARepository,
        QCResultRepository,
    )
except Exception:  # pragma: no cover - persistence layer unavailable
    AIPredictionRepository = None  # type: ignore
    CAPARepository = None  # type: ignore
    QCResultRepository = None  # type: ignore

try:
    from cloud.api.middleware.correlation import (  # type: ignore
        CORRELATION_HEADER,
        AuditLoggingMiddleware,
        CorrelationIdMiddleware,
    )
except Exception:  # pragma: no cover - container layout
    # Service-dir-only build context: fall back to the local copy that ships
    # alongside this module so the image is self-contained.
    from middleware import (  # type: ignore
        CORRELATION_HEADER,
        AuditLoggingMiddleware,
        CorrelationIdMiddleware,
    )


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
SERVICE_NAME = "qc-evaluation"
VERSION = "0.1.0"
API_PREFIX = "/api/v1"


def _utcnow() -> datetime:
    """Timezone-aware UTC now."""
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Prometheus metrics (default REGISTRY)
# --------------------------------------------------------------------------- #
QC_EVALUATIONS_TOTAL = Counter(
    "qconnect_qc_evaluations_total",
    "Total QC evaluations performed, by analyte type and resulting status.",
    ["analyte_type", "qc_status"],
)
QC_SEVERITY_TOTAL = Counter(
    "qconnect_qc_severity_total",
    "Total QC evaluations by resulting severity.",
    ["severity"],
)
QC_EVALUATION_DURATION = Histogram(
    "qconnect_qc_evaluation_duration_seconds",
    "Duration of the QC evaluate endpoint in seconds.",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)
ML_INFERENCE_UP = Gauge(
    "qconnect_ml_inference_up",
    "1 when the last ML inference call succeeded, 0 when degraded.",
)
HTTP_REQUESTS_TOTAL = Counter(
    "qconnect_http_requests_total",
    "Total HTTP requests served, by method, path and status code.",
    ["method", "path", "status"],
)
CAPA_AUTODRAFTED_TOTAL = Counter(
    "qconnect_capa_autodrafted_total",
    "Total CAPA records auto-drafted by the evaluator on a FAIL result.",
)


# Singleton engine instances (stateless, cheap to reuse).
_westgard = WestgardEngine()
_qconnect = QConnectEngine()
_sigma = SigmaEngine()
_distribution = DistributionDetector()


# --------------------------------------------------------------------------- #
# Lifespan
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Async lifespan: configure logging on startup, clean up on shutdown."""
    _configure_logging()
    logger.info("Starting {} v{}", SERVICE_NAME, VERSION)
    try:
        yield
    finally:
        logger.info("Shutting down {} v{}", SERVICE_NAME, VERSION)
        # Best-effort cleanup of the DB pool if one was opened.
        try:
            from cloud.config.database import dispose_engine

            await dispose_engine()
        except Exception as exc:  # pragma: no cover
            logger.debug("No DB engine to dispose: {}", exc)


def _configure_logging() -> None:
    """Configure loguru for structured, level-controlled output."""
    settings = _safe_settings()
    level = getattr(settings, "log_level", "INFO") if settings else "INFO"
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        backtrace=False,
        diagnose=False,
        enqueue=False,
        format=(
            "{time:YYYY-MM-DDTHH:mm:ss.SSSZ} | {level: <8} | "
            "{extra[correlation_id]} | {name}:{function}:{line} - {message}"
        ),
    )
    # Default the bound correlation_id so the format string never KeyErrors.
    logger.configure(extra={"correlation_id": "-"})


def _safe_settings() -> Any:
    """Return settings or ``None`` without raising."""
    try:
        return get_app_settings()
    except Exception:  # pragma: no cover
        return None


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
app = FastAPI(
    title="QConnect-AI :: QC Evaluation",
    version=VERSION,
    description=(
        "Orchestrates Westgard / QConnect / Sigma engines plus AI enrichment "
        "into a unified QC verdict."
    ),
    lifespan=lifespan,
)
app.add_middleware(AuditLoggingMiddleware)
app.add_middleware(CorrelationIdMiddleware)


@app.middleware("http")
async def _metrics_middleware(request: Request, call_next: Any) -> Response:
    """Time every request and count it by method, route template and status.

    Uses the matched route path template (not the raw URL) to keep label
    cardinality bounded. Never swallows the downstream response.
    """
    started = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - started
    route = request.scope.get("route")
    path = getattr(route, "path", None) or request.url.path
    if path != "/metrics":
        HTTP_REQUESTS_TOTAL.labels(
            method=request.method,
            path=path,
            status=str(response.status_code),
        ).inc()
        if path == f"{API_PREFIX}/qc/evaluate":
            QC_EVALUATION_DURATION.observe(elapsed)
    return response


# --------------------------------------------------------------------------- #
# Exception handlers
# --------------------------------------------------------------------------- #
@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Return a structured 422 for request validation failures."""
    corr_id = getattr(request.state, "correlation_id", None)
    logger.warning("Validation error on {}: {}", request.url.path, exc.errors())
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": "validation_error",
            "message": "Request payload failed validation",
            "details": exc.errors(),
            "correlation_id": corr_id,
        },
    )


@app.exception_handler(Exception)
async def _generic_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all handler returning a structured 500.

    ``HTTPException`` is re-raised semantics: FastAPI handles those separately,
    so this only fires for unexpected errors.
    """
    corr_id = getattr(request.state, "correlation_id", None)
    logger.exception("Unhandled error on {}: {}", request.url.path, exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "internal_error",
            "message": "An unexpected error occurred",
            "correlation_id": corr_id,
        },
    )


# --------------------------------------------------------------------------- #
# Core orchestration
# --------------------------------------------------------------------------- #
async def _call_ml_inference(
    qc: "schemas.QCDataInput", correlation_id: str | None
) -> dict[str, Any] | None:
    """Call the ML-inference service (best-effort).

    Returns the parsed JSON payload, or ``None`` on any failure (timeout,
    connection refused, non-2xx). The orchestrator degrades gracefully when the
    AI layer is unavailable.
    """
    settings = _safe_settings()
    base_url = getattr(settings, "ml_inference_url", "http://ml-inference:8001")
    timeout = float(getattr(settings, "ml_inference_timeout_seconds", 2.0))
    url = f"{base_url.rstrip('/')}/predict"
    headers: dict[str, str] = {}
    if correlation_id:
        headers[CORRELATION_HEADER] = correlation_id
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=qc.model_dump(mode="json"), headers=headers)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        logger.warning("ML inference unavailable ({}); degrading: {}", url, exc)
        return None


async def _call_rca_capa(
    qc: "schemas.QCDataInput",
    response: "schemas.QCEvaluationResponse",
    correlation_id: str | None,
) -> dict[str, Any] | None:
    """Draft a CAPA via the rca_capa service (best-effort).

    POSTs to ``{rca_capa_url}/capa`` with a body compatible with the service's
    ``CAPARequest`` schema (analyte_code, analyte_type, qc_status,
    westgard_rule_violated, severity, incident_date). Returns the drafted-CAPA
    dict, or ``None`` on any failure so a down rca_capa never fails evaluation.
    """
    settings = _safe_settings()
    base_url = getattr(settings, "rca_capa_url", "http://rca-capa:8003")
    timeout = float(getattr(settings, "rca_capa_timeout_seconds", 2.0))
    url = f"{base_url.rstrip('/')}/capa"

    westgard = (response.legacy_results or {}).get("westgard") or {}
    body = {
        "analyte_code": qc.analyte_code,
        "analyte_type": qc.analyte_type.value,
        "qc_status": response.qc_status.value,
        "westgard_rule_violated": westgard.get("rule_violated"),
        "severity": response.severity.value,
        "incident_date": _utcnow().isoformat(),
    }
    headers: dict[str, str] = {}
    if correlation_id:
        headers[CORRELATION_HEADER] = correlation_id
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        logger.warning("rca_capa unavailable ({}); skipping CAPA: {}", url, exc)
        return None


def _fuse(
    westgard: dict,
    qconnect: dict,
    sigma: dict,
    distribution: dict,
    ml_payload: dict[str, Any] | None,
    analyte_type: str,
) -> tuple[str, str, "schemas.AIInsights", str, float]:
    """Fuse all engine outputs into ``(qc_status, severity, ai_insights,
    recommendation, confidence)``.

    Fusion rules:
        * any FAIL -> FAIL
        * any REVIEW_REQUIRED or engine discordance -> REVIEW_REQUIRED
        * else PASS
    """
    # Per-run verdict is governed by Westgard (rules) and QConnect (limits).
    # Sigma is a *method-capability* metric: per Westgard-Sigma methodology it
    # drives rule selection, severity and recommendations, not a single-run
    # reject, so it is intentionally excluded from the gating statuses. Its
    # advisory verdict is still surfaced on the sigma fragment for transparency.
    statuses = [westgard["status"], qconnect["status"]]
    sigma.setdefault("advisory_status", sigma_status(sigma))
    discordance = _qconnect.compare_with_westgard(qconnect, westgard)

    if "FAIL" in statuses:
        qc_status = "FAIL"
    elif "REVIEW_REQUIRED" in statuses or discordance["discordance_detected"]:
        qc_status = "REVIEW_REQUIRED"
    else:
        qc_status = "PASS"

    # AI enrichment (graceful defaults if ML is down).
    ml = ml_payload or {}
    ai = schemas.AIInsights(
        distribution_type=distribution.get("detected_distribution", "unknown"),
        failure_probability_48h=ml.get("failure_probability_48h"),
        anomaly_detected=bool(ml.get("anomaly_detected", False)),
        anomaly_score=ml.get("anomaly_score"),
        diagnostic_sigma=sigma.get("diagnostic_sigma"),
        clinical_impact_percent=ml.get("clinical_impact_percent"),
        recommended_action=ml.get("recommended_action"),
    )

    # If AI flags an imminent failure but legacy says PASS, hold for review.
    fp48 = ai.failure_probability_48h
    if qc_status == "PASS" and fp48 is not None and fp48 >= 0.8:
        qc_status = "HOLD_PENDING_AI"

    severity = _map_severity(qc_status, westgard, analyte_type, ai)
    recommendation = _build_recommendation(qc_status, westgard, sigma, discordance, ai)
    confidence = _confidence(distribution, ml_payload is not None)
    return qc_status, severity, ai, recommendation, confidence


def sigma_status(sigma: dict) -> str:
    """Derive a PASS/REVIEW status from the sigma category.

    Sigma is a *method capability* metric, not a per-run reject, so it never
    FAILs a single result. A sub-3 method is flagged for review.
    """
    label = sigma.get("sigma_category", ">6")
    if label in ("<2", "2-3"):
        return "REVIEW_REQUIRED"
    return "PASS"


def _map_severity(
    qc_status: str,
    westgard: dict,
    analyte_type: str,
    ai: "schemas.AIInsights",
) -> str:
    """Map the fused status + context onto a severity level."""
    if qc_status == "PASS":
        return "LOW"

    # Random-error rejections and critical analytes escalate.
    random_error_rules = {"1-3S", "R-4S"}
    rule = westgard.get("rule_violated")
    critical_analytes = {"serology", "nat"}

    if qc_status == "FAIL":
        if rule in random_error_rules or analyte_type in critical_analytes:
            return "CRITICAL"
        return "HIGH"
    if qc_status == "HOLD_PENDING_AI":
        return "HIGH"
    # REVIEW_REQUIRED
    if ai.anomaly_detected:
        return "MEDIUM"
    return "MEDIUM"


def _build_recommendation(
    qc_status: str,
    westgard: dict,
    sigma: dict,
    discordance: dict,
    ai: "schemas.AIInsights",
) -> str:
    """Compose a human-readable recommendation string."""
    parts: list[str] = []
    if qc_status == "FAIL":
        rule = westgard.get("rule_violated")
        parts.append(
            f"QC rejected (Westgard {rule}). Halt patient reporting for this "
            "analyte, investigate, repeat QC after corrective action."
        )
    elif qc_status == "HOLD_PENDING_AI":
        parts.append(
            "Legacy rules pass but AI predicts imminent failure (>=80% in 48h). "
            "Hold and pre-emptively service / recalibrate."
        )
    elif qc_status == "REVIEW_REQUIRED":
        parts.append("Manual review recommended.")
        if discordance.get("discordance_detected"):
            parts.append(discordance["recommendation"])
    else:
        parts.append("In control. Continue routine operation.")

    if sigma.get("sigma_category") in ("<2", "2-3"):
        parts.append(
            f"Method sigma is {sigma.get('sigma_category')} — apply rule set: "
            f"{sigma.get('recommended_rules')}."
        )
    if ai.recommended_action:
        parts.append(f"AI: {ai.recommended_action}")
    return " ".join(parts)


def _confidence(distribution: dict, ai_available: bool) -> float:
    """Combine distribution-detection confidence with AI availability."""
    base = float(distribution.get("confidence", 0.5))
    if ai_available:
        base = min(1.0, base + 0.1)
    return round(max(0.0, min(1.0, base)), 4)


def _evaluate_engines(qc: "schemas.QCDataInput", history: list[float]) -> dict:
    """Run all deterministic engines and return their raw fragments.

    Wrapped so the caller can translate engine failures into a clean HTTP 500.
    """
    westgard = _westgard.evaluate(qc.result_value, qc.target_value, qc.sd_value, history)
    qconnect = _qconnect.evaluate(qc.result_value, {"history": history} if history else {})
    bias_percent = (
        (qc.target_value - (sum(history) / len(history))) / qc.target_value * 100.0
        if history and qc.target_value
        else 0.0
    )
    cv_percent = westgard["cv_percent"] or (
        qc.sd_value / qc.target_value * 100.0 if qc.target_value else 0.0
    )
    # Default allowable error of 10% if not supplied with the lot.
    sigma = _sigma.evaluate(
        bias_percent=bias_percent,
        cv_percent=cv_percent,
        allowable_error_percent=10.0,
    )
    distribution = _distribution.detect(history)
    return {
        "westgard": westgard,
        "qconnect": qconnect,
        "sigma": sigma,
        "distribution": distribution,
    }


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.post(
    f"{API_PREFIX}/qc/evaluate",
    response_model=schemas.QCEvaluationResponse,
    tags=["evaluation"],
    summary="Evaluate a single QC result through all engines + AI.",
)
async def evaluate_qc(
    qc: "schemas.QCDataInput",
    request: Request,
    db: Any = Depends(get_db),
    principal: dict | None = Depends(get_current_principal),
) -> "schemas.QCEvaluationResponse":
    """Evaluate one QC result and return the fused verdict.

    Pipeline: load history (best-effort from DB) -> run Westgard / QConnect /
    Sigma / distribution engines -> call ML inference (best-effort) -> fuse ->
    persist (best-effort) -> respond.

    Raises:
        HTTPException(500): if a deterministic engine raises unexpectedly.
    """
    corr_id = qc.correlation_id or getattr(request.state, "correlation_id", None)

    history = await _load_history(db, qc)

    try:
        engines = _evaluate_engines(qc, history)
    except Exception as exc:
        logger.exception("Engine failure: {}", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "engine_error", "message": str(exc)},
        ) from exc

    ml_payload = await _call_ml_inference(qc, corr_id)
    ML_INFERENCE_UP.set(1 if ml_payload is not None else 0)

    qc_status, severity, ai, recommendation, confidence = _fuse(
        engines["westgard"],
        engines["qconnect"],
        engines["sigma"],
        engines["distribution"],
        ml_payload,
        qc.analyte_type.value,
    )

    response = schemas.QCEvaluationResponse(
        qc_status=schemas.QCStatusEnum(qc_status),
        severity=schemas.SeverityEnum(severity),
        legacy_results={
            "westgard": engines["westgard"],
            "qconnect": engines["qconnect"],
            "sigma": engines["sigma"],
            "distribution": engines["distribution"],
        },
        ai_insights=ai,
        recommendation=recommendation,
        confidence=confidence,
        correlation_id=corr_id,
        evaluated_offline=False,
        timestamp=_utcnow(),
    )

    qc_result_id = await _persist_result(db, qc, response)

    # --- Auto-CAPA on FAIL (best-effort; rca_capa down must NOT fail eval). --- #
    settings = _safe_settings()
    auto_capa = bool(getattr(settings, "auto_capa", True))
    if response.qc_status == schemas.QCStatusEnum.FAIL and auto_capa:
        capa = await _call_rca_capa(qc, response, corr_id)
        if capa is not None:
            response.capa = capa
            CAPA_AUTODRAFTED_TOTAL.inc()
            if db is not None and CAPARepository is not None:
                try:
                    await CAPARepository(db).create(capa, qc_result_id)
                    await db.commit()
                except Exception as exc:
                    logger.warning("CAPA persist failed; rolling back: {}", exc)
                    try:
                        await db.rollback()
                    except Exception:  # pragma: no cover - defensive
                        pass

    # --- Metrics: count evaluations + severity. The request-duration histogram
    # is observed by the HTTP middleware for the evaluate route. ------------- #
    QC_EVALUATIONS_TOTAL.labels(
        analyte_type=qc.analyte_type.value,
        qc_status=response.qc_status.value,
    ).inc()
    QC_SEVERITY_TOTAL.labels(severity=response.severity.value).inc()

    logger.info(
        "Evaluated {} {} -> {} ({})",
        qc.analyte_code,
        qc.qc_level.value,
        qc_status,
        severity,
    )
    return response


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Expose Prometheus metrics in the text exposition format."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get(
    f"{API_PREFIX}/labs/{{lab_id}}/qc/status",
    response_model=schemas.LabQCStatusSummary,
    tags=["status"],
    summary="Recent QC status summary for a lab (placeholder).",
)
async def lab_qc_status(
    lab_id: str,
    db: Any = Depends(get_db),
    principal: dict | None = Depends(get_current_principal),
) -> "schemas.LabQCStatusSummary":
    """Return a rolling-window status summary for a lab.

    PLACEHOLDER: in production this aggregates counts from the ``qc_results``
    table over the last ``window_hours``. With no DB configured it returns an
    empty summary rather than failing.
    """
    if db is None or QCResultRepository is None:
        return schemas.LabQCStatusSummary(lab_id=lab_id)
    try:
        counts = await QCResultRepository(db).lab_status_counts(lab_id)
        return schemas.LabQCStatusSummary(**counts)
    except Exception as exc:
        logger.warning("lab status aggregation failed: {}", exc)
        return schemas.LabQCStatusSummary(lab_id=lab_id)


@app.post(
    f"{API_PREFIX}/labs/{{lab_id}}/qc/batch",
    response_model=schemas.QCBatchAccepted,
    tags=["sync"],
    summary="Accept a batch of QC results synced from an edge node.",
)
async def ingest_batch(
    lab_id: str,
    batch: "schemas.QCBatchUpload",
    request: Request,
    db: Any = Depends(get_db),
    principal: dict | None = Depends(get_current_principal),
) -> "schemas.QCBatchAccepted":
    """Ingest a batch of QC results (the edge sync target).

    Each record is evaluated and persisted best-effort. Records that fail
    evaluation are counted as rejected rather than aborting the whole batch.
    """
    corr_id = getattr(request.state, "correlation_id", None)
    accepted = 0
    rejected = 0
    for record in batch.records:
        try:
            history = await _load_history(db, record)
            engines = _evaluate_engines(record, history)
            qc_status, severity, ai, recommendation, confidence = _fuse(
                engines["westgard"],
                engines["qconnect"],
                engines["sigma"],
                engines["distribution"],
                None,  # batch path skips per-record ML to stay fast
                record.analyte_type.value,
            )
            resp = schemas.QCEvaluationResponse(
                qc_status=schemas.QCStatusEnum(qc_status),
                severity=schemas.SeverityEnum(severity),
                legacy_results={
                    "westgard": engines["westgard"],
                    "qconnect": engines["qconnect"],
                    "sigma": engines["sigma"],
                },
                ai_insights=ai,
                recommendation=recommendation,
                confidence=confidence,
                correlation_id=corr_id,
                evaluated_offline=False,
            )
            await _persist_result(db, record, resp)
            accepted += 1
        except Exception as exc:
            logger.warning("Rejected batch record: {}", exc)
            rejected += 1
    logger.info("Batch from lab {}: accepted={} rejected={}", lab_id, accepted, rejected)
    return schemas.QCBatchAccepted(
        lab_id=lab_id,
        accepted=accepted,
        rejected=rejected,
        correlation_id=corr_id,
    )


@app.get("/health", response_model=schemas.HealthStatus, tags=["ops"])
async def health() -> "schemas.HealthStatus":
    """Liveness/readiness probe.

    Performs best-effort reachability checks for the DB and Redis but never
    raises — a failing dependency is reported as ``False`` rather than crashing.
    """
    checks: dict[str, bool] = {
        "db": await _check_db(),
        "redis": await _check_redis(),
    }
    overall = "ok"
    return schemas.HealthStatus(
        status=overall,
        service=SERVICE_NAME,
        version=VERSION,
        checks=checks,
    )


# --------------------------------------------------------------------------- #
# Best-effort persistence / lookup helpers (stubs over the DB session)
# --------------------------------------------------------------------------- #
async def _load_history(db: Any, qc: "schemas.QCDataInput") -> list[float]:
    """Load recent control values for this analyte/lot from ``qc_results``.

    Best-effort: returns ``[]`` when no DB is configured or on any query error,
    so the engines always receive a (possibly empty) chronological history.
    """
    if db is None or QCResultRepository is None:
        return []
    try:
        repo = QCResultRepository(db)
        return await repo.recent_history(qc.analyte_code, qc.qc_lot_id)
    except Exception as exc:
        logger.debug("history load failed: {}", exc)
        return []


async def _persist_result(
    db: Any, qc: "schemas.QCDataInput", response: "schemas.QCEvaluationResponse"
) -> str | None:
    """Persist an evaluated result and its AI prediction (best-effort).

    Writes a ``qc_results`` row, then an ``ai_predictions`` row when AI data is
    present, and commits. Returns the new qc_results id (as a string) so the
    caller can link a CAPA; returns ``None`` without a DB or on any failure
    (after rolling back). Never raises.
    """
    if db is None or QCResultRepository is None:
        return None
    try:
        qc_dict = qc.model_dump(mode="json")
        resp_dict = response.model_dump(mode="json")
        qc_result = await QCResultRepository(db).create(qc_dict, resp_dict)
        ai = resp_dict.get("ai_insights") or {}
        if ai and AIPredictionRepository is not None:
            await AIPredictionRepository(db).create(qc_result.id, ai)
        await db.commit()
        return str(qc_result.id)
    except Exception as exc:
        logger.warning("persist failed; rolling back: {}", exc)
        try:
            await db.rollback()
        except Exception:  # pragma: no cover - defensive
            pass
        return None


async def _check_db() -> bool:
    """Best-effort DB reachability check."""
    try:
        from cloud.config.database import get_sessionmaker
        from sqlalchemy import text  # type: ignore

        sm = get_sessionmaker()
        if sm is None:
            return False
        async with sm() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def _check_redis() -> bool:
    """Best-effort Redis reachability check."""
    try:
        import redis.asyncio as aioredis  # type: ignore

        settings = _safe_settings()
        url = getattr(settings, "redis_url", "redis://localhost:6379/0")
        client = aioredis.from_url(url, socket_connect_timeout=0.5)
        pong = await client.ping()
        await client.aclose()
        return bool(pong)
    except Exception:
        return False


# Wire the Authorization header into the auth dependency for real requests.
async def _auth_header_dep(
    authorization: str | None = Header(default=None),
) -> dict | None:
    """FastAPI wrapper that feeds the raw Authorization header to the auth dep."""
    return await get_current_principal(authorization)


# Override the placeholder dependency used in the route signatures so the header
# is actually injected by FastAPI at request time.
app.dependency_overrides[get_current_principal] = _auth_header_dep


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
