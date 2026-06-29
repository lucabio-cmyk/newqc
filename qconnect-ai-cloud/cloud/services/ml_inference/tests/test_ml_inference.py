"""Tests for the ML-inference service (offline, deterministic)."""

from __future__ import annotations

from fastapi.testclient import TestClient

import main
from main import app

client = TestClient(app)


# Required response keys read by the qc_evaluation orchestrator.
_REQUIRED_KEYS = {
    "failure_probability_48h",
    "anomaly_detected",
    "anomaly_score",
    "clinical_impact_percent",
    "recommended_action",
}


def _base_payload(**overrides: object) -> dict:
    """A full QCDataInput-shaped payload; overridable per test."""
    payload = {
        "lab_id": "lab-genova-001",
        "analyzer_id": "ABBOTT-ARCHITECT-001",
        "analyte_code": "HCV-AB",
        "analyte_type": "serology",
        "qc_lot_id": "QC-HCV-DIAMEX-202603-001",
        "qc_level": "NORMAL",
        "result_value": 1.50,
        "target_value": 1.50,
        "sd_value": 0.08,
        "operator_id": "EMP00234",
        "timestamp": "2026-03-15T09:30:00Z",
    }
    payload.update(overrides)
    return payload


def test_health_ok() -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "ml-inference"
    assert body["version"] == main.VERSION
    assert "checks" in body


def test_predict_stable_history_low_risk() -> None:
    # A stable, on-target history -> low failure probability, no anomaly.
    stable = [1.50, 1.49, 1.51, 1.50, 1.48, 1.52, 1.50, 1.49, 1.51, 1.50]
    resp = client.post(
        "/predict",
        json=_base_payload(result_value=1.50, target_value=1.50, history=stable),
    )
    assert resp.status_code == 200
    body = resp.json()

    # All required keys present.
    assert _REQUIRED_KEYS.issubset(body.keys())

    # Correct types.
    assert isinstance(body["failure_probability_48h"], float)
    assert isinstance(body["anomaly_detected"], bool)
    assert isinstance(body["anomaly_score"], float)
    assert isinstance(body["clinical_impact_percent"], float)
    assert isinstance(body["recommended_action"], str)

    assert 0.0 <= body["failure_probability_48h"] <= 1.0
    assert body["failure_probability_48h"] < 0.3
    assert body["failure_risk_level"] == "low"
    assert body["anomaly_detected"] is False


def test_predict_upward_trend_and_outlier_raises_risk() -> None:
    # Strong upward drift plus a far-from-target current value.
    trending = [1.50, 1.55, 1.62, 1.70, 1.79, 1.88, 1.97, 2.05, 2.14, 2.23]
    stable = [1.50, 1.49, 1.51, 1.50, 1.48, 1.52, 1.50, 1.49, 1.51, 1.50]

    stable_resp = client.post(
        "/predict",
        json=_base_payload(result_value=1.50, target_value=1.50, history=stable),
    ).json()
    trend_resp = client.post(
        "/predict",
        json=_base_payload(result_value=2.40, target_value=1.50, history=trending),
    ).json()

    # Trending/outlier case should be riskier and/or flagged anomalous.
    assert trend_resp["failure_probability_48h"] > stable_resp["failure_probability_48h"]
    assert trend_resp["failure_probability_48h"] >= 0.3 or trend_resp["anomaly_detected"] is True


def test_predict_outlier_anomaly_detected() -> None:
    stable = [1.50, 1.49, 1.51, 1.50, 1.48, 1.52, 1.50, 1.49, 1.51, 1.50]
    resp = client.post(
        "/predict",
        json=_base_payload(result_value=5.0, target_value=1.50, history=stable),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["anomaly_detected"] is True
    assert body["anomaly_score"] > 0.0


def test_predict_lenient_extra_field() -> None:
    resp = client.post(
        "/predict",
        json=_base_payload(some_future_field="surprise", another={"nested": 1}),
    )
    assert resp.status_code == 200
    assert _REQUIRED_KEYS.issubset(resp.json().keys())


def test_predict_minimal_payload() -> None:
    # Even an almost-empty body should not 422 (lenient defaults).
    resp = client.post("/predict", json={})
    assert resp.status_code == 200
    assert _REQUIRED_KEYS.issubset(resp.json().keys())


def test_metrics_contains_predictions_total() -> None:
    client.post("/predict", json=_base_payload(history=[1.5, 1.5, 1.5, 1.5]))
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "qconnect_ml_predictions_total" in resp.text
