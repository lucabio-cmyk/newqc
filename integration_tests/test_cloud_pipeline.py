"""End-to-end tests through the REAL cloud app via the in-process client.

These drive the actual FastAPI ``qc_evaluation`` service (engines + fusion +
metrics + middleware), using the deterministic ``shared.testing`` factories for
the request bodies. They run fully offline: the ML-inference call degrades
gracefully when unreachable.
"""

from __future__ import annotations

from shared.testing import (
    make_failing_qc_input,
    make_qc_history,
    make_qc_input,
)

API_PREFIX = "/api/v1"

_VALID_STATUS = {"PASS", "FAIL", "REVIEW_REQUIRED", "HOLD_PENDING_AI"}
_VALID_SEVERITY = {"CRITICAL", "HIGH", "MEDIUM", "LOW"}


def _post_evaluate(client, qc):
    """POST a QCDataInput (pydantic) and return the response object."""
    return client.post(f"{API_PREFIX}/qc/evaluate", json=qc.model_dump(mode="json"))


def test_cold_start_single_evaluate_passes(cloud_client) -> None:
    """A cold-start in-spec value with no history evaluates to PASS."""
    qc = make_qc_input()
    resp = _post_evaluate(cloud_client, qc)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["qc_status"] == "PASS"
    assert body["severity"] in _VALID_SEVERITY
    assert "westgard" in body["legacy_results"]
    assert "qconnect" in body["legacy_results"]
    assert "sigma" in body["legacy_results"]


def test_failing_value_fails_with_1_3s(cloud_client) -> None:
    """A >+3 SD value fails the fused verdict via the Westgard 1-3S rule."""
    qc = make_failing_qc_input()
    resp = _post_evaluate(cloud_client, qc)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["qc_status"] == "FAIL"
    assert body["legacy_results"]["westgard"]["rule_violated"] == "1-3S"
    assert body["legacy_results"]["westgard"]["deviation_sd"] > 3.0


def test_batch_ingest_accepted_count(cloud_client) -> None:
    """A batch of several records reports a matching accepted count."""
    records = [make_qc_input(operator_id=f"EMP{i:05d}").model_dump(mode="json") for i in range(5)]
    payload = {"lab_id": "lab-genova-001", "records": records}
    resp = cloud_client.post(f"{API_PREFIX}/labs/lab-genova-001/qc/batch", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] == 5
    assert body["rejected"] == 0


def test_built_up_history_sequence_stays_valid(cloud_client) -> None:
    """Feed a built-up history through evaluate calls; shape stays valid."""
    history = make_qc_history(mean=1.50, sd=0.08, n=12, seed=42)
    statuses: list[str] = []
    for value in history:
        qc = make_qc_input(result_value=max(value, 0.0001))
        resp = _post_evaluate(cloud_client, qc)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["qc_status"] in _VALID_STATUS
        assert body["severity"] in _VALID_SEVERITY
        assert 0.0 <= body["confidence"] <= 1.0
        statuses.append(body["qc_status"])
    # In-control gaussian data around target should be predominantly PASS.
    assert statuses.count("PASS") >= len(statuses) // 2


def test_metrics_reflect_activity(cloud_client) -> None:
    """/metrics exposes the evaluation counter after an evaluate call."""
    assert _post_evaluate(cloud_client, make_qc_input()).status_code == 200
    resp = cloud_client.get("/metrics")
    assert resp.status_code == 200
    body = resp.text
    assert "qconnect_qc_evaluations_total" in body
    assert "qconnect_qc_severity_total" in body


def test_correlation_id_is_echoed(cloud_client) -> None:
    """A supplied X-Correlation-ID is echoed back on the response."""
    corr = "integration-corr-0001"
    resp = cloud_client.post(
        f"{API_PREFIX}/qc/evaluate",
        json=make_qc_input().model_dump(mode="json"),
        headers={"X-Correlation-ID": corr},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("X-Correlation-ID") == corr
    # The correlation id also propagates into the response body.
    assert resp.json()["correlation_id"] == corr
