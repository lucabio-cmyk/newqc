"""Pytest fixtures for the QConnect-AI cloud test suite.

Ensures the qc_evaluation service directory and the repo root are importable, and
provides shared fixtures: a sample QCDataInput dict and a Starlette/FastAPI
``TestClient`` bound to the qc_evaluation app.

All fixtures are designed to work WITHOUT a database or network — the ML call in
the service is best-effort and degrades when unreachable.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# --------------------------------------------------------------------------- #
# Path wiring
# --------------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parents[2]  # …/qconnect-ai-cloud
_SERVICE_DIR = _REPO_ROOT / "cloud" / "services" / "qc_evaluation"

for p in (_REPO_ROOT, _SERVICE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


@pytest.fixture()
def sample_qc_data() -> dict:
    """A valid QCDataInput payload (HCV serology, normal level)."""
    return {
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


@pytest.fixture()
def failing_qc_data() -> dict:
    """A QCDataInput that trips the Westgard 1-3S rule (> +3 SD)."""
    return {
        "lab_id": "lab-genova-001",
        "analyzer_id": "ABBOTT-ARCHITECT-001",
        "analyte_code": "TROPONIN-I",
        "analyte_type": "chemistry",
        "qc_lot_id": "QC-TNI-202603-001",
        "qc_level": "NORMAL",
        "result_value": 0.053,
        "target_value": 0.040,
        "sd_value": 0.004,
        "operator_id": "EMP00234",
        "timestamp": "2026-03-15T09:30:00Z",
    }


@pytest.fixture()
def client():
    """Return a TestClient for the qc_evaluation FastAPI app.

    Skips the whole test module if FastAPI / its test deps are not installed in
    the current environment.
    """
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    # Import the app only after path wiring above is in place.
    from cloud.services.qc_evaluation import main as qc_main  # noqa: WPS433

    return fastapi_testclient.TestClient(qc_main.app)
