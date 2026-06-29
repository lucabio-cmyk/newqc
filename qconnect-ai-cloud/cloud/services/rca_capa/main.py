"""RCA / CAPA FastAPI service.

When a QC result FAILS, this service suggests ranked probable *root causes*
using a clinical-lab knowledge graph and drafts a *CAPA* (Corrective And
Preventive Action) record.

The knowledge graph is pluggable. By default an in-memory, pure-stdlib graph
(:class:`graph.knowledge_graph.KnowledgeGraph`) is used. When ``NEO4J_URI`` is
configured and the ``neo4j`` driver is importable, a Neo4j-backed adapter is
used instead (see :mod:`graph.neo4j_adapter`).

Endpoints:
    * ``POST /rca``   -> ranked candidate root causes
    * ``POST /capa``  -> runs RCA then drafts a CAPA record
    * ``GET  /health``-> liveness + graph-backend availability
    * ``GET  /metrics``-> Prometheus exposition

Import resilience: works whether the service dir is on ``sys.path`` (container)
or imported as ``cloud.services.rca_capa`` (monorepo / tests).
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI
from fastapi.responses import Response
from loguru import logger
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# Import resilience: make sibling modules importable in either layout.
# --------------------------------------------------------------------------- #
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))
_REPO_ROOT = _THIS_DIR.parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:  # Container-style imports (service dir on path).
    from capa import draft_capa  # type: ignore
    from graph.neo4j_adapter import get_graph  # type: ignore
except Exception:  # pragma: no cover - monorepo/test layout
    from cloud.services.rca_capa.capa import draft_capa  # type: ignore
    from cloud.services.rca_capa.graph.neo4j_adapter import get_graph  # type: ignore


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
SERVICE_NAME = "rca-capa"
VERSION = "0.1.0"


def _utcnow() -> datetime:
    """Timezone-aware UTC now (non-deterministic; never used for asserted fields)."""
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Settings (lightweight; reads env, no hard pydantic-settings dependency)
# --------------------------------------------------------------------------- #
class _Settings:
    """Minimal settings read from the environment.

    Kept dependency-free so the graph factory can introspect Neo4j config
    without coupling the service to pydantic-settings.
    """

    def __init__(self) -> None:
        self.neo4j_uri: str | None = os.getenv("NEO4J_URI") or None
        self.neo4j_user: str | None = os.getenv("NEO4J_USER") or None
        self.neo4j_password: str | None = os.getenv("NEO4J_PASSWORD") or None
        self.neo4j_database: str | None = os.getenv("NEO4J_DATABASE") or None
        self.log_level: str = os.getenv("LOG_LEVEL", "INFO")


def get_settings() -> _Settings:
    """Return freshly-read settings."""
    return _Settings()


# --------------------------------------------------------------------------- #
# Prometheus metrics (default REGISTRY)
# --------------------------------------------------------------------------- #
RCA_REQUESTS_TOTAL = Counter(
    "qconnect_rca_requests_total",
    "Total RCA inference requests served.",
)
CAPA_DRAFTED_TOTAL = Counter(
    "qconnect_capa_drafted_total",
    "Total CAPA records drafted, by severity.",
    ["severity"],
)
RCA_DURATION = Histogram(
    "qconnect_rca_duration_seconds",
    "Duration of RCA inference in seconds.",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)
RCA_GRAPH_UP = Gauge(
    "qconnect_rca_graph_up",
    "1 when the RCA knowledge-graph backend is available, 0 otherwise.",
)


# --------------------------------------------------------------------------- #
# Graph backend (resolved at startup, refreshable)
# --------------------------------------------------------------------------- #
_graph: Any = None


def _resolve_graph() -> Any:
    """Resolve (and cache) the RCA graph backend for the current settings."""
    global _graph
    _graph = get_graph(get_settings())
    RCA_GRAPH_UP.set(1 if getattr(_graph, "available", True) else 0)
    return _graph


def _get_graph() -> Any:
    """Return the cached graph backend, resolving it on first use."""
    if _graph is None:
        return _resolve_graph()
    return _graph


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class RCARequest(BaseModel):
    """Context for a root-cause suggestion request.

    Lenient: unknown symptom keys are accepted (``extra="allow"``) and passed
    straight through to the knowledge graph, which ignores keys it does not
    model.
    """

    model_config = ConfigDict(extra="allow")

    analyte_code: str = "UNKNOWN"
    analyte_type: str | None = None
    qc_status: str = "FAIL"
    westgard_rule_violated: str | None = None
    shift_or_trend: str | None = None
    reagent_lot_age_days: float | None = None


class CAPARequest(RCARequest):
    """An RCA request plus the optional incident date used for CAPA dates."""

    incident_date: str | None = Field(
        default=None,
        description="ISO date/datetime of the incident; CAPA dates are offset from it.",
    )


class RankedCause(BaseModel):
    """A single ranked candidate root cause."""

    cause: str
    category: str
    confidence: float
    evidence: list[str]
    corrective_action: str
    preventive_action: str


class RCAResponse(BaseModel):
    """Ranked candidate root causes for a QC failure."""

    ranked_causes: list[RankedCause]
    graph_backend: str


def _symptoms_from_request(req: RCARequest) -> dict[str, Any]:
    """Flatten an RCA request (including extra fields) into a symptom dict."""
    data = req.model_dump()
    # Normalise analyte_type for graph predicates (lower-case match in graph).
    if data.get("analyte_type"):
        data["analyte_type"] = str(data["analyte_type"]).lower()
    return data


# --------------------------------------------------------------------------- #
# Lifespan + logging
# --------------------------------------------------------------------------- #
def _configure_logging() -> None:
    """Configure loguru for structured, level-controlled output."""
    level = get_settings().log_level
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        backtrace=False,
        diagnose=False,
        enqueue=False,
        format=(
            "{time:YYYY-MM-DDTHH:mm:ss.SSSZ} | {level: <8} | "
            "{name}:{function}:{line} - {message}"
        ),
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Configure logging and resolve the graph backend on startup."""
    _configure_logging()
    graph = _resolve_graph()
    logger.info(
        "Starting {} v{} (graph backend: {})",
        SERVICE_NAME,
        VERSION,
        getattr(graph, "backend_name", "in-memory"),
    )
    try:
        yield
    finally:
        logger.info("Shutting down {} v{}", SERVICE_NAME, VERSION)
        closer = getattr(graph, "close", None)
        if callable(closer):  # pragma: no cover - only the Neo4j adapter has one
            try:
                closer()
            except Exception as exc:  # pragma: no cover
                logger.debug("Graph close failed: {}", exc)


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
app = FastAPI(
    title="QConnect-AI :: RCA / CAPA",
    version=VERSION,
    description=(
        "Suggests ranked root causes for QC failures from a clinical-lab "
        "knowledge graph and drafts CAPA records."
    ),
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.post("/rca", response_model=RCAResponse, tags=["rca"])
async def suggest_root_cause(req: RCARequest) -> RCAResponse:
    """Return ranked candidate root causes for a QC failure."""
    RCA_REQUESTS_TOTAL.inc()
    graph = _get_graph()
    started = time.perf_counter()
    ranked = graph.infer(_symptoms_from_request(req))
    RCA_DURATION.observe(time.perf_counter() - started)
    logger.info(
        "RCA for {} ({}): {} candidate cause(s)",
        req.analyte_code,
        req.westgard_rule_violated or "no-rule",
        len(ranked),
    )
    return RCAResponse(
        ranked_causes=[RankedCause(**c) for c in ranked],
        graph_backend=getattr(graph, "backend_name", "in-memory"),
    )


@app.post("/capa", tags=["capa"])
async def draft_capa_endpoint(req: CAPARequest) -> dict[str, Any]:
    """Run RCA for the failure then draft a CAPA record."""
    RCA_REQUESTS_TOTAL.inc()
    graph = _get_graph()
    started = time.perf_counter()
    ranked = graph.infer(_symptoms_from_request(req))
    RCA_DURATION.observe(time.perf_counter() - started)

    capa = draft_capa(req.model_dump(), ranked)
    CAPA_DRAFTED_TOTAL.labels(severity=capa["severity"]).inc()
    logger.info(
        "Drafted {} (severity={}) for {}",
        capa["capa_number"],
        capa["severity"],
        req.analyte_code,
    )
    return capa


@app.get("/health", tags=["ops"])
async def health() -> dict[str, Any]:
    """Liveness probe including graph-backend availability."""
    graph = _get_graph()
    backend = getattr(graph, "backend_name", "in-memory")
    available = bool(getattr(graph, "available", True))
    RCA_GRAPH_UP.set(1 if available else 0)
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "version": VERSION,
        "graph_backend": backend,
        "checks": {"graph": available},
        "timestamp": _utcnow().isoformat(),
    }


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Expose Prometheus metrics in the text exposition format."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8003)
