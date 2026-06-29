"""End-to-end persistence tests for the qc_evaluation service.

These run against an in-memory aiosqlite database (the ``client_with_db``
fixture). They are fully offline/deterministic: the ML-inference and rca_capa
HTTP calls are best-effort and degrade when unreachable, so no network is used
unless explicitly monkeypatched.
"""

from __future__ import annotations

from sqlalchemy import func, select

from cloud.db.models import AIPrediction, CAPAAction, QCResult

API_PREFIX = "/api/v1"


def _count(client, model) -> int:
    """Count rows of ``model`` on the client's portal loop (shared DB)."""

    async def _run(session) -> int:
        result = await session.execute(select(func.count()).select_from(model))
        return int(result.scalar_one())

    return client.run_db(_run)


def test_evaluate_persists_qc_result(client_with_db, sample_qc_data) -> None:
    """A successful evaluate POST writes a qc_results row."""
    resp = client_with_db.post(f"{API_PREFIX}/qc/evaluate", json=sample_qc_data)
    assert resp.status_code == 200, resp.text
    assert _count(client_with_db, QCResult) == 1


def test_history_accumulates_across_requests(client_with_db, sample_qc_data) -> None:
    """A second evaluate for the same analyte sees the first row persisted.

    StaticPool keeps the in-memory DB alive across requests, so the row count
    increases monotonically (proving cross-request persistence).
    """
    client_with_db.post(f"{API_PREFIX}/qc/evaluate", json=sample_qc_data)
    assert _count(client_with_db, QCResult) == 1
    client_with_db.post(f"{API_PREFIX}/qc/evaluate", json=sample_qc_data)
    assert _count(client_with_db, QCResult) == 2


def test_ai_prediction_persisted(client_with_db, sample_qc_data) -> None:
    """Each evaluate also writes an ai_predictions row (AI insights present)."""
    client_with_db.post(f"{API_PREFIX}/qc/evaluate", json=sample_qc_data)
    assert _count(client_with_db, AIPrediction) == 1


def test_lab_status_reflects_persisted_rows(client_with_db, sample_qc_data) -> None:
    """The lab status endpoint reports total_results > 0 after evaluations."""
    client_with_db.post(f"{API_PREFIX}/qc/evaluate", json=sample_qc_data)
    client_with_db.post(f"{API_PREFIX}/qc/evaluate", json=sample_qc_data)
    resp = client_with_db.get(f"{API_PREFIX}/labs/lab-genova-001/qc/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_results"] == 2
    assert body["pass_count"] >= 1
    assert "PASS" in body["status_counts"]


def test_fail_persists_without_rca_capa(client_with_db, failing_qc_data) -> None:
    """A FAIL still returns 200 and persists the row when rca_capa is down.

    No monkeypatch: the rca_capa HTTP call fails (unreachable) and is swallowed,
    so ``capa`` is None but the qc_results row is written.
    """
    resp = client_with_db.post(f"{API_PREFIX}/qc/evaluate", json=failing_qc_data)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["qc_status"] == "FAIL"
    assert body["capa"] is None
    assert _count(client_with_db, QCResult) == 1
    assert _count(client_with_db, CAPAAction) == 0


def test_fail_drafts_and_persists_capa(client_with_db, failing_qc_data, monkeypatch) -> None:
    """With a fake rca_capa, the CAPA is persisted + attached + metric increments."""
    from cloud.services.qc_evaluation import main as qc_main

    fake_capa = {
        "capa_number": "CAPA-TROPONIN-I-DEADBEEF",
        "status": "OPEN",
        "severity": "CRITICAL",
        "root_cause_category": "calibration",
        "root_cause_description": "Calibration drift",
        "ai_suggested_causes": [{"cause": "drift", "confidence": 0.9}],
        "ai_rca_confidence": 0.9,
        "immediate_action": "Recalibrate",
        "preventive_action": "Schedule maintenance",
        "incident_date": "2026-03-15",
        "target_closure_date": "2026-03-18",
        "monitoring_period_days": 30,
    }

    async def _fake_rca(qc, response, corr_id):
        return fake_capa

    monkeypatch.setattr(qc_main, "_call_rca_capa", _fake_rca)

    before = qc_main.CAPA_AUTODRAFTED_TOTAL._value.get()
    resp = client_with_db.post(f"{API_PREFIX}/qc/evaluate", json=failing_qc_data)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["capa"] is not None
    assert body["capa"]["capa_number"] == "CAPA-TROPONIN-I-DEADBEEF"
    assert _count(client_with_db, CAPAAction) == 1
    assert qc_main.CAPA_AUTODRAFTED_TOTAL._value.get() == before + 1


def test_batch_persists_accepted_rows(client_with_db, sample_qc_data) -> None:
    """Batch ingest persists one qc_results row per accepted record."""
    payload = {"lab_id": "lab-genova-001", "records": [sample_qc_data, sample_qc_data]}
    resp = client_with_db.post(f"{API_PREFIX}/labs/lab-genova-001/qc/batch", json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["accepted"] == 2
    assert _count(client_with_db, QCResult) == 2
