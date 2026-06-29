"""API-level tests for the qc_evaluation service.

These exercise the FastAPI app end-to-end via TestClient. They run fully offline:
the ML-inference call inside the service is best-effort and degrades gracefully
when unreachable, so the happy-path test passes without any network.
"""

from __future__ import annotations

API_PREFIX = "/api/v1"


def test_health(client) -> None:
    """/health returns 200 with service identity and a checks map."""
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "qc-evaluation"
    assert "version" in body
    assert "checks" in body
    # Without infra the checks are simply False, never an error.
    assert set(body["checks"]) >= {"db", "redis"}


def test_evaluate_happy_path(client, sample_qc_data) -> None:
    """POST /qc/evaluate returns a well-formed QCEvaluationResponse."""
    resp = client.post(f"{API_PREFIX}/qc/evaluate", json=sample_qc_data)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    valid_status = {"PASS", "FAIL", "REVIEW_REQUIRED", "HOLD_PENDING_AI"}
    valid_severity = {"CRITICAL", "HIGH", "MEDIUM", "LOW"}

    assert body["qc_status"] in valid_status
    assert body["severity"] in valid_severity
    assert "ai_insights" in body
    assert 0.0 <= body["confidence"] <= 1.0
    assert "westgard" in body["legacy_results"]
    assert "qconnect" in body["legacy_results"]
    assert "sigma" in body["legacy_results"]
    # An in-spec value close to target should pass.
    assert body["qc_status"] == "PASS"


def test_evaluate_failure_case(client, failing_qc_data) -> None:
    """A >+3 SD value must be rejected (FAIL) by the fused verdict."""
    resp = client.post(f"{API_PREFIX}/qc/evaluate", json=failing_qc_data)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["qc_status"] == "FAIL"
    assert body["legacy_results"]["westgard"]["rule_violated"] == "1-3S"


def test_evaluate_validation_error(client) -> None:
    """Missing required fields yield a structured 422."""
    resp = client.post(f"{API_PREFIX}/qc/evaluate", json={"lab_id": "x"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "validation_error"


def test_batch_ingest(client, sample_qc_data) -> None:
    """Batch endpoint accepts records and reports an accepted count."""
    payload = {
        "lab_id": "lab-genova-001",
        "records": [sample_qc_data, sample_qc_data],
    }
    resp = client.post(f"{API_PREFIX}/labs/lab-genova-001/qc/batch", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] == 2
    assert body["rejected"] == 0


def test_lab_status_placeholder(client) -> None:
    """Lab status summary returns an empty-but-valid summary without a DB."""
    resp = client.get(f"{API_PREFIX}/labs/lab-genova-001/qc/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["lab_id"] == "lab-genova-001"
    assert body["total_results"] == 0


def test_correlation_id_echoed(client, sample_qc_data) -> None:
    """An inbound X-Correlation-ID is echoed back on the response."""
    resp = client.post(
        f"{API_PREFIX}/qc/evaluate",
        json=sample_qc_data,
        headers={"X-Correlation-ID": "test-corr-123"},
    )
    assert resp.status_code == 200
    assert resp.headers.get("X-Correlation-ID") == "test-corr-123"
