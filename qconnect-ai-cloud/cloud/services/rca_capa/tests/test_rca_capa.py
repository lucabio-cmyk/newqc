"""Tests for the RCA / CAPA service. Offline, deterministic, in-memory graph."""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

_SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVICE_DIR))

from capa import draft_capa  # noqa: E402
from graph.knowledge_graph import KnowledgeGraph  # noqa: E402
from main import app  # noqa: E402

client = TestClient(app)


def _confidences_descending(causes: list[dict]) -> bool:
    confs = [c["confidence"] for c in causes]
    return all(0.0 <= c <= 1.0 for c in confs) and confs == sorted(confs, reverse=True)


# --------------------------------------------------------------------------- #
# Unit: KnowledgeGraph.infer
# --------------------------------------------------------------------------- #
def test_knowledge_graph_random_error_serology() -> None:
    graph = KnowledgeGraph()
    ranked = graph.infer(
        {
            "westgard_rule_violated": "1-3S",
            "analyte_type": "serology",
            "shift_or_trend": "single",
            "reagent_lot_age_days": 80,
        }
    )
    assert ranked, "expected at least one cause"
    assert _confidences_descending(ranked)
    # Top cause must be a random-error-consistent cause.
    assert ranked[0]["category"] in {"reagent", "instrument", "operator"}


def test_knowledge_graph_trend_ranks_calibration_high() -> None:
    graph = KnowledgeGraph()
    ranked = graph.infer(
        {
            "westgard_rule_violated": "10x",
            "shift_or_trend": "trend",
            "days_since_calibration": 45,
        }
    )
    assert ranked
    assert _confidences_descending(ranked)
    categories = [c["category"] for c in ranked]
    # Calibration drift should rank in the top two for a systematic trend.
    assert "calibration" in categories[:2]


def test_knowledge_graph_no_match_returns_empty() -> None:
    graph = KnowledgeGraph()
    assert graph.infer({}) == []


def test_seeded_causes_have_actions() -> None:
    graph = KnowledgeGraph()
    causes = graph.causes()
    assert len(causes) >= 6
    for c in causes:
        assert c.corrective_action and c.preventive_action
        assert c.edges


# --------------------------------------------------------------------------- #
# API: /rca
# --------------------------------------------------------------------------- #
def test_rca_random_error_serology_top_cause() -> None:
    resp = client.post(
        "/rca",
        json={
            "analyte_code": "HBSAG",
            "analyte_type": "serology",
            "qc_status": "FAIL",
            "westgard_rule_violated": "1-3S",
            "shift_or_trend": "single",
            "reagent_lot_age_days": 85,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["graph_backend"] == "in-memory"
    causes = body["ranked_causes"]
    assert causes
    assert _confidences_descending(causes)
    assert causes[0]["category"] in {"reagent", "instrument", "operator"}


def test_rca_trend_ranks_calibration() -> None:
    resp = client.post(
        "/rca",
        json={
            "analyte_code": "GLU",
            "analyte_type": "chemistry",
            "qc_status": "FAIL",
            "westgard_rule_violated": "10x",
            "shift_or_trend": "trend",
            "days_since_calibration": 40,
        },
    )
    assert resp.status_code == 200
    causes = resp.json()["ranked_causes"]
    assert causes
    assert _confidences_descending(causes)
    categories = [c["category"] for c in causes]
    assert "calibration" in categories[:2]


def test_rca_accepts_extra_fields() -> None:
    resp = client.post(
        "/rca",
        json={
            "analyte_code": "TSH",
            "qc_status": "FAIL",
            "westgard_rule_violated": "1-3S",
            "some_unmodeled_symptom": "whatever",
        },
    )
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# API: /capa
# --------------------------------------------------------------------------- #
def _capa_payload() -> dict:
    return {
        "analyte_code": "HBSAG",
        "analyte_type": "serology",
        "qc_status": "FAIL",
        "westgard_rule_violated": "1-3S",
        "shift_or_trend": "single",
        "reagent_lot_age_days": 85,
        "incident_date": "2026-06-29",
    }


def test_capa_well_formed() -> None:
    resp = client.post("/capa", json=_capa_payload())
    assert resp.status_code == 200
    capa = resp.json()
    for key in (
        "capa_number",
        "severity",
        "ai_rca_confidence",
        "immediate_action",
        "preventive_action",
        "status",
        "target_closure_date",
        "monitoring_period_days",
    ):
        assert key in capa, f"missing {key}"
    assert capa["status"] == "OPEN"
    assert capa["capa_number"].startswith("CAPA-HBSAG-")
    assert 0.0 <= capa["ai_rca_confidence"] <= 1.0
    # 1-3S + serology -> CRITICAL.
    assert capa["severity"] == "CRITICAL"
    # CRITICAL closure offset is 3 days from incident.
    assert capa["target_closure_date"] == "2026-07-02"


def test_capa_number_deterministic() -> None:
    a = client.post("/capa", json=_capa_payload()).json()
    b = client.post("/capa", json=_capa_payload()).json()
    assert a["capa_number"] == b["capa_number"]
    assert a["target_closure_date"] == b["target_closure_date"]


def test_capa_unit_no_causes_fallback() -> None:
    capa = draft_capa({"analyte_code": "X", "qc_status": "FAIL"}, [])
    assert capa["root_cause_category"] == "unknown"
    assert capa["ai_rca_confidence"] == 0.0
    assert capa["immediate_action"]
    assert capa["preventive_action"]


# --------------------------------------------------------------------------- #
# API: /health and /metrics
# --------------------------------------------------------------------------- #
def test_health_in_memory_backend() -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["graph_backend"] == "in-memory"
    assert body["checks"]["graph"] is True


def test_metrics_contains_request_counter() -> None:
    client.post("/rca", json={"analyte_code": "A", "westgard_rule_violated": "1-3S"})
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "qconnect_rca_requests_total" in resp.text
