"""RCA / CAPA FastAPI service (placeholder).

Exposes ``POST /api/v1/rca/suggest`` returning ranked candidate root causes for a
QC failure. Production queries a Neo4j failure-knowledge graph; this scaffold
returns a fixed, domain-plausible ranking.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from pydantic import BaseModel

VERSION = "0.1.0"
SERVICE_NAME = "rca-capa"

app = FastAPI(title="QConnect-AI :: RCA / CAPA", version=VERSION)


class RCARequest(BaseModel):
    """Context for a root-cause suggestion request."""

    model_config = {"extra": "ignore"}

    analyte_code: str = "UNKNOWN"
    westgard_rule_violated: str | None = None
    severity: str = "MEDIUM"


class RootCauseCandidate(BaseModel):
    """A single ranked candidate root cause."""

    category: str
    description: str
    confidence: float


class RCAResponse(BaseModel):
    """Ranked candidate root causes for a failure."""

    candidates: list[RootCauseCandidate]
    model_note: str = "placeholder graph-free heuristic"


# Rule -> typical root causes (illustrative knowledge-graph stand-in).
_RULE_CAUSES: dict[str, list[tuple[str, str, float]]] = {
    "1-3S": [
        ("random_error", "Pipetting / bubble / sample integrity issue", 0.55),
        ("instrument", "Detector or optics transient fault", 0.30),
    ],
    "2-2S": [
        ("calibration", "Calibration drift / shifted lot setpoint", 0.6),
        ("reagent", "New reagent lot bias", 0.35),
    ],
    "4-1S": [
        ("calibration", "Slow calibration drift", 0.6),
        ("maintenance", "Component ageing (lamp, electrode)", 0.3),
    ],
    "10x": [
        ("reagent", "Reagent lot systematic bias", 0.55),
        ("calibration", "Recalibration needed", 0.35),
    ],
    "7T": [
        ("maintenance", "Progressive drift (reagent ageing / fouling)", 0.6),
        ("environment", "Temperature/humidity trend", 0.3),
    ],
}


@app.post("/api/v1/rca/suggest", response_model=RCAResponse, tags=["rca"])
async def suggest_root_cause(req: RCARequest) -> RCAResponse:
    """Return ranked candidate root causes (placeholder)."""
    rule = req.westgard_rule_violated or ""
    raw = _RULE_CAUSES.get(
        rule,
        [("unknown", "Insufficient signal — perform structured RCA", 0.4)],
    )
    candidates = [
        RootCauseCandidate(category=c, description=d, confidence=conf) for c, d, conf in raw
    ]
    return RCAResponse(candidates=candidates)


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

    uvicorn.run(app, host="0.0.0.0", port=8003)
