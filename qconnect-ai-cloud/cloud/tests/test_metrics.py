"""Tests for the Prometheus ``/metrics`` endpoint on the qc-evaluation service.

These tests are fully offline-safe: the ML inference call degrades gracefully
when unreachable, so an evaluate POST succeeds without any network/DB.
"""

from __future__ import annotations


def test_metrics_endpoint_returns_plaintext(client) -> None:
    """GET /metrics returns 200 with a Prometheus text content type."""
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")


def test_metrics_expose_qc_families_after_evaluate(client, sample_qc_data) -> None:
    """After one evaluate call the QC metric families appear in the scrape."""
    eval_resp = client.post("/api/v1/qc/evaluate", json=sample_qc_data)
    assert eval_resp.status_code == 200

    resp = client.get("/metrics")
    assert resp.status_code == 200
    body = resp.text
    assert "qconnect_qc_evaluations_total" in body
    assert "qconnect_qc_evaluation_duration_seconds" in body
    assert "qconnect_qc_severity_total" in body
    assert "qconnect_ml_inference_up" in body
