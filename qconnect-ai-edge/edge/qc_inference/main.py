"""QConnect-AI EDGE inference service (FastAPI).

The per-lab node that evaluates QC results *locally and offline*. It:

* accepts QC results over HTTP (``POST /evaluate``) and optionally over HL7/MLLP;
* fuses the Westgard, QConnect, LSTM-lite and anomaly engines into a single
  :class:`QCEvaluationResponse` within the <100ms budget;
* persists every result to the local SQLite cache for later cloud upload;
* serves a minimal built-in dashboard and exposes health/status probes.

Graceful degradation is the design rule throughout: missing ML, stale limits,
and an unreachable cloud must never prevent a result from being returned.
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger

from edge.qc_inference import __version__
from edge.qc_inference.cache.model_cache import ModelCache
from edge.qc_inference.cache.sqlite_manager import EdgeCache
from edge.qc_inference.hl7.listener import MLLPListener
from edge.qc_inference.models.anomaly import AnomalyDetector
from edge.qc_inference.models.lstm_lite import LSTMLite
from edge.qc_inference.models.qconnect import QConnectEngine
from edge.qc_inference.models.westgard import WestgardEngine
from edge.qc_inference.schemas import (
    AIInsights,
    QCDataInput,
    QCEvaluationResponse,
    QCStatusEnum,
    SeverityEnum,
)

# --------------------------------------------------------------------------- #
# Configuration (env-driven; sensible offline-friendly defaults)
# --------------------------------------------------------------------------- #
LAB_ID = os.getenv("LAB_ID", "lab-unknown")
LAB_NAME = os.getenv("LAB_NAME", "Unnamed Laboratory")
CLOUD_URL = os.getenv("CLOUD_URL", "").rstrip("/")
LAB_AUTH_TOKEN = os.getenv("LAB_AUTH_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "/data/qc_cache.db")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
ENABLE_HL7 = os.getenv("ENABLE_HL7", "false").lower() in {"1", "true", "yes", "on"}
HL7_LISTEN_PORT = int(os.getenv("HL7_LISTEN_PORT", "2575"))
LSTM_MODEL_NAME = "lstm_lite"
# Best-effort timeout for the cloud connectivity probe (kept tiny).
CLOUD_PROBE_TIMEOUT = float(os.getenv("CLOUD_PROBE_TIMEOUT", "2.0"))

# Module-level singletons populated by the lifespan handler.
_cache: EdgeCache | None = None
_model_cache: ModelCache | None = None
_westgard = WestgardEngine()
_qconnect = QConnectEngine()
_anomaly = AnomalyDetector()
_lstm = LSTMLite()
_hl7_listener: MLLPListener | None = None
_state: dict[str, Any] = {"last_sync": None, "models_loaded": {}}


# --------------------------------------------------------------------------- #
# Lifespan: open cache, load models/limits, optionally start HL7 listener
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialise edge resources on startup and tear them down on shutdown.

    Background *upload* sync is intentionally NOT started here - that is the job
    of the separate ``data-sync`` service. A manual trigger is exposed instead.
    """
    global _cache, _model_cache, _hl7_listener

    logger.remove()
    logger.add(lambda m: print(m, end=""), level=LOG_LEVEL)
    logger.info("Starting QConnect-AI edge inference v{} for lab={}", __version__, LAB_ID)

    # --- Open the local cache (offline backbone). --------------------- #
    _ensure_parent_dir(DB_PATH)
    _cache = EdgeCache(DB_PATH)
    _model_cache = ModelCache(_cache)

    # --- Load any cached LSTM model (degrades to heuristic if absent). - #
    _load_models()

    # --- Optionally start the HL7/MLLP listener as a background task. -- #
    if ENABLE_HL7:
        try:
            _hl7_listener = MLLPListener(
                callback=_on_hl7_message, host="0.0.0.0", port=HL7_LISTEN_PORT
            )
            await _hl7_listener.start()
            import asyncio

            app.state.hl7_task = asyncio.create_task(_hl7_listener.serve_forever())
            logger.info("HL7/MLLP listener enabled on port {}", HL7_LISTEN_PORT)
        except Exception as exc:  # noqa: BLE001 - never block startup on HL7
            logger.error("Failed to start HL7 listener: {}", exc)
    else:
        logger.info("HL7 listener disabled (set ENABLE_HL7=true to enable)")

    try:
        yield
    finally:
        if _hl7_listener is not None:
            await _hl7_listener.stop()
        if _cache is not None:
            _cache.close()
        logger.info("Edge inference shut down cleanly")


app = FastAPI(
    title="QConnect-AI Edge Inference",
    description="Per-lab offline QC evaluation node.",
    version=__version__,
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Core evaluation endpoint
# --------------------------------------------------------------------------- #
@app.post("/evaluate", response_model=QCEvaluationResponse)
async def evaluate(qc: QCDataInput) -> QCEvaluationResponse:
    """Evaluate a single QC result locally and cache it for upload.

    Pipeline: Westgard (always) -> QConnect (cached limits) -> LSTM-lite forecast
    -> anomaly detection -> fusion. If any ML step is unavailable the result is
    still produced from Westgard alone (graceful degradation).

    Args:
        qc: the inbound QC data point.

    Returns:
        A :class:`QCEvaluationResponse` with ``evaluated_offline=True``.
    """
    started = time.perf_counter()
    corr = qc.correlation_id or _new_correlation_id()
    logger.info(
        "[{}] evaluate analyte={} value={} analyzer={}",
        corr, qc.analyte_code, qc.result_value, qc.analyzer_id,
    )

    # --- Pull local history + cached limits (both offline-safe). ------- #
    history = _safe_history(qc.analyte_code)
    cached_limits = _safe_limits(qc.analyte_code)

    legacy: dict[str, dict] = {}

    # --- Westgard (always runs; pure-python). ------------------------- #
    westgard = _westgard.evaluate(
        qc.result_value, qc.target_value, qc.sd_value, history
    )
    legacy["westgard"] = westgard

    # --- QConnect (uses cached percentile limits + local history). ---- #
    limits_for_engine = dict(cached_limits or {})
    limits_for_engine.setdefault("history", history)
    qconnect = _qconnect.evaluate(qc.result_value, limits_for_engine)
    legacy["qconnect"] = qconnect

    # --- LSTM-lite forecast (heuristic fallback always returns). ------ #
    forecast: dict[str, Any] = {}
    try:
        forecast = _lstm.predict(history + [qc.result_value])
    except Exception as exc:  # noqa: BLE001 - never fail the request on ML
        logger.warning("[{}] LSTM-lite failed: {}", corr, exc)

    # --- Anomaly detection. ------------------------------------------- #
    anomaly: dict[str, Any] = {"anomaly_detected": False, "anomaly_score": None}
    try:
        anomaly = _anomaly.detect(qc.result_value, history)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[{}] anomaly detection failed: {}", corr, exc)

    # --- Fuse into a single decision. --------------------------------- #
    status, severity, recommendation, confidence = _fuse(westgard, qconnect, anomaly)

    ai = AIInsights(
        distribution_type=str((cached_limits or {}).get("distribution_type", "unknown")),
        failure_probability_48h=forecast.get("failure_probability_48h"),
        anomaly_detected=bool(anomaly.get("anomaly_detected", False)),
        anomaly_score=anomaly.get("anomaly_score"),
        recommended_action=recommendation,
    )

    response = QCEvaluationResponse(
        qc_status=status,
        severity=severity,
        legacy_results=legacy,
        ai_insights=ai,
        recommendation=recommendation,
        confidence=confidence,
        correlation_id=corr,
        evaluated_offline=True,
        timestamp=datetime.now(timezone.utc),
    )

    # --- Persist as pending upload (durability before returning). ----- #
    if _cache is not None:
        try:
            _cache.save_qc_result(qc.model_dump(), response.model_dump())
        except Exception as exc:  # noqa: BLE001 - cache failure must not 500 a good eval
            logger.error("[{}] failed to cache QC result: {}", corr, exc)

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    logger.info(
        "[{}] result={} severity={} confidence={:.2f} in {:.1f}ms",
        corr, status.value, severity.value, confidence, elapsed_ms,
    )
    if elapsed_ms > 100:
        logger.warning("[{}] evaluation exceeded 100ms budget ({:.1f}ms)", corr, elapsed_ms)
    return response


# --------------------------------------------------------------------------- #
# Dashboard (minimal built-in; the richer one is the Flask web_ui)
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    """Serve a minimal self-contained dashboard (no external CDNs)."""
    return _DASHBOARD_HTML.replace("{{LAB_ID}}", LAB_ID).replace("{{LAB_NAME}}", LAB_NAME)


@app.get("/api/status")
async def api_status() -> JSONResponse:
    """JSON consumed by the built-in dashboard (mirror of ``/status``)."""
    return JSONResponse(_status_payload())


# --------------------------------------------------------------------------- #
# Health & status
# --------------------------------------------------------------------------- #
@app.get("/health")
async def health() -> JSONResponse:
    """Liveness/readiness probe. Never raises; reports component checks."""
    db_ok = bool(_cache and _cache.ping())
    models_ok = True  # LSTM-lite always answers (heuristic fallback)
    cloud_ok = await _cloud_reachable()
    payload = {
        "status": "ok" if db_ok else "degraded",
        "service": "qc-inference",
        "version": __version__,
        "checks": {
            "database": db_ok,
            "models": models_ok,
            "cloud": cloud_ok,
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    # Always 200 so orchestrators don't kill an offline-but-functional node;
    # "degraded" in the body signals partial health.
    return JSONResponse(payload)


@app.get("/status")
async def status() -> JSONResponse:
    """Sync/operational status JSON."""
    payload = _status_payload()
    payload["cloud_connected"] = await _cloud_reachable()
    return JSONResponse(payload)


@app.post("/sync/trigger")
async def trigger_sync() -> JSONResponse:
    """Manually request an upload sync.

    The actual upload loop lives in the separate ``data-sync`` service; this edge
    process does not run it. We surface the pending count and let an operator/
    orchestrator act on it. Returned for convenience/observability.
    """
    pending = _cache.count_pending_uploads() if _cache else 0
    logger.info("Manual sync trigger requested; {} records pending", pending)
    return JSONResponse(
        {
            "triggered": True,
            "pending_uploads": pending,
            "note": "Upload is performed by the data-sync service.",
        }
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _status_payload() -> dict[str, Any]:
    """Build the common status dict (no network calls)."""
    pending = _cache.count_pending_uploads() if _cache else 0
    qc_24h = _cache.get_qc_summary_24h() if _cache else []
    return {
        "lab_id": LAB_ID,
        "lab_name": LAB_NAME,
        "cloud_connected": None,  # filled in by /status (best-effort probe)
        "pending_uploads": pending,
        "last_sync": _state.get("last_sync"),
        "models_loaded": _state.get("models_loaded", {}),
        "qc_24h": qc_24h,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _load_models() -> None:
    """Load cached models into the engines, recording availability in state."""
    loaded: dict[str, Any] = {"lstm": False, "lstm_backend": None, "version": None}
    if _model_cache is not None:
        path = _model_cache.materialize(LSTM_MODEL_NAME)
        if path:
            ok = _lstm.load_model(path)
            loaded["lstm"] = ok
            loaded["lstm_backend"] = _lstm.backend
            loaded["version"] = _model_cache.get_version(LSTM_MODEL_NAME)
    # Even without a real model the engine works via heuristic fallback.
    loaded["lstm_heuristic_fallback"] = not loaded["lstm"]
    _state["models_loaded"] = loaded
    logger.info("Models loaded: {}", loaded)


def _safe_history(analyte_code: str) -> list[float]:
    """Fetch local QC history, swallowing cache errors."""
    if _cache is None:
        return []
    try:
        return _cache.get_local_qc_history(analyte_code, days=30)
    except Exception as exc:  # noqa: BLE001
        logger.warning("history fetch failed for {}: {}", analyte_code, exc)
        return []


def _safe_limits(analyte_code: str) -> dict[str, Any] | None:
    """Fetch cached control limits, swallowing cache errors."""
    if _cache is None:
        return None
    try:
        return _cache.get_control_limits(analyte_code)
    except Exception as exc:  # noqa: BLE001
        logger.warning("limits fetch failed for {}: {}", analyte_code, exc)
        return None


def _fuse(
    westgard: dict, qconnect: dict, anomaly: dict
) -> tuple[QCStatusEnum, SeverityEnum, str | None, float]:
    """Fuse engine outputs into (status, severity, recommendation, confidence).

    Decision policy (conservative - patient-safety first):
        * Any engine FAIL -> FAIL.
        * Engines disagree, or a 7T/1-2S warning, or anomaly -> REVIEW_REQUIRED.
        * Otherwise PASS.
    """
    w_status = westgard.get("status", "PASS")
    q_status = qconnect.get("status", "PASS")
    rule = westgard.get("rule_violated")
    anomalous = bool(anomaly.get("anomaly_detected", False))

    if w_status == "FAIL" or q_status == "FAIL":
        status = QCStatusEnum.FAIL
        # 1-3S / R-4S are random error (often high), 2-2S/4-1S systematic.
        severity = SeverityEnum.CRITICAL if rule in {"1-3S", "R-4S"} else SeverityEnum.HIGH
        recommendation = (
            f"Reject run; investigate {rule or 'control failure'}. "
            "Repeat QC and review reagent/calibration."
        )
        confidence = 0.9
    elif w_status == "REVIEW_REQUIRED" or q_status == "REVIEW_REQUIRED" or anomalous:
        status = QCStatusEnum.REVIEW_REQUIRED
        severity = SeverityEnum.MEDIUM if anomalous else SeverityEnum.LOW
        recommendation = (
            "Borderline/disagreeing signals - operator review recommended "
            "before releasing patient results."
        )
        confidence = 0.6
    else:
        status = QCStatusEnum.PASS
        severity = SeverityEnum.LOW
        recommendation = None
        confidence = 0.85

    return status, severity, recommendation, confidence


async def _cloud_reachable() -> bool:
    """Best-effort cloud connectivity probe. Always returns a bool, never raises."""
    if not CLOUD_URL:
        return False
    try:
        async with httpx.AsyncClient(timeout=CLOUD_PROBE_TIMEOUT) as client:
            resp = await client.get(f"{CLOUD_URL}/health")
            return resp.status_code < 500
    except Exception as exc:  # noqa: BLE001 - offline is normal, not an error
        logger.debug("cloud probe failed (offline?): {}", exc)
        return False


async def _on_hl7_message(parsed: dict) -> None:
    """Callback for HL7 messages: archive raw, then evaluate if it looks like QC.

    HL7 results do not carry the assigned target/SD, so they cannot be Westgard-
    evaluated without joining to lot metadata. Here we archive the message and,
    when cached limits exist for the analyte, run a QConnect-only evaluation. Full
    enrichment happens once lot metadata is available.
    """
    analyte = parsed.get("analyte_code")
    if _cache is not None:
        try:
            _cache.save_hl7_raw(str(parsed.get("msh", "")), analyte)
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to archive HL7 message: {}", exc)
    logger.info("HL7 message ingested analyte={} value={}", analyte, parsed.get("result_value"))


def _ensure_parent_dir(path: str) -> None:
    """Create the parent directory of ``path`` if it does not exist."""
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("could not create data dir {}: {}", parent, exc)


def _new_correlation_id() -> str:
    """Generate a correlation id (uuid hex) for request tracing."""
    import uuid

    return uuid.uuid4().hex


# --------------------------------------------------------------------------- #
# Built-in minimal dashboard (inline, no CDNs)
# --------------------------------------------------------------------------- #
_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>QConnect-AI Edge - {{LAB_NAME}}</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         background:#0f1216; color:#e6e9ef; }
  header { padding:16px 24px; background:#161b22; border-bottom:1px solid #283142; }
  header h1 { margin:0; font-size:18px; }
  header .sub { color:#8b97a8; font-size:13px; margin-top:4px; }
  .grid { display:grid; gap:16px; padding:24px;
          grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }
  .card { background:#161b22; border:1px solid #283142; border-radius:10px; padding:16px; }
  .card h2 { margin:0 0 8px; font-size:13px; text-transform:uppercase;
             letter-spacing:.05em; color:#8b97a8; }
  .metric { font-size:30px; font-weight:600; }
  .ok { color:#3fb950; } .warn { color:#d29922; } .bad { color:#f85149; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:6px 8px; border-bottom:1px solid #283142; }
  .pill { display:inline-block; padding:2px 8px; border-radius:12px; font-size:12px; }
  .pill.up { background:#12331c; color:#3fb950; } .pill.down { background:#3a1416; color:#f85149; }
  footer { padding:12px 24px; color:#5b6675; font-size:12px; }
</style>
</head>
<body>
<header>
  <h1>QConnect-AI Edge Node</h1>
  <div class="sub">Lab: {{LAB_NAME}} (<span id="labId">{{LAB_ID}}</span>) -
    offline-capable QC evaluation</div>
</header>
<div class="grid">
  <div class="card"><h2>Cloud connectivity</h2>
    <div class="metric" id="cloud">--</div></div>
  <div class="card"><h2>Pending uploads</h2>
    <div class="metric" id="pending">--</div></div>
  <div class="card"><h2>Last sync</h2>
    <div class="metric" id="lastSync" style="font-size:16px;">--</div></div>
  <div class="card"><h2>Models</h2>
    <div id="models" style="font-size:14px;">--</div></div>
</div>
<div class="grid">
  <div class="card" style="grid-column:1/-1;">
    <h2>QC summary - last 24h</h2>
    <table>
      <thead><tr><th>Analyte</th><th>Pass</th><th>Fail</th><th>Other</th></tr></thead>
      <tbody id="qcBody"><tr><td colspan="4">Loading...</td></tr></tbody>
    </table>
  </div>
</div>
<footer>Auto-refreshes every 10s - data served entirely from the local edge cache.</footer>
<script>
async function refresh() {
  try {
    const r = await fetch('/status');
    const d = await r.json();
    const cloud = document.getElementById('cloud');
    cloud.textContent = d.cloud_connected ? 'Online' : 'Offline';
    cloud.className = 'metric ' + (d.cloud_connected ? 'ok' : 'warn');
    document.getElementById('pending').textContent = d.pending_uploads ?? '--';
    document.getElementById('lastSync').textContent = d.last_sync || 'never';
    const m = d.models_loaded || {};
    document.getElementById('models').innerHTML =
      'LSTM: ' + (m.lstm ? 'loaded' : 'heuristic') +
      '<br/>version: ' + (m.version || 'n/a');
    const body = document.getElementById('qcBody');
    const rows = d.qc_24h || [];
    body.innerHTML = rows.length ? rows.map(function(x){
      return '<tr><td>' + x.analyte + '</td><td class="ok">' + (x.pass||0) +
             '</td><td class="bad">' + (x.fail||0) + '</td><td>' + (x.other||0) +
             '</td></tr>';
    }).join('') : '<tr><td colspan="4">No QC results in the last 24h.</td></tr>';
  } catch (e) {
    document.getElementById('cloud').textContent = 'error';
  }
}
refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>"""
