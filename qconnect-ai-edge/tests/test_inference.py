"""Tests for the local inference engines and the SQLite cache.

Restricted to pure-stdlib + pydantic modules (no FastAPI / TensorFlow / numpy):
WestgardEngine, QConnectEngine, AnomalyDetector, LSTMLite (heuristic fallback)
and EdgeCache.
"""

from __future__ import annotations

import os

from edge.qc_inference.cache.sqlite_manager import EdgeCache
from edge.qc_inference.models.anomaly import AnomalyDetector
from edge.qc_inference.models.lstm_lite import LSTMLite
from edge.qc_inference.models.qconnect import QConnectEngine
from edge.qc_inference.models.westgard import WestgardEngine


# --------------------------------------------------------------------------- #
# Westgard
# --------------------------------------------------------------------------- #
def test_westgard_pass() -> None:
    eng = WestgardEngine()
    # In-control point near the mean, stable history.
    res = eng.evaluate(10.1, target=10.0, sd=0.5, history=[10.0, 9.9, 10.1, 10.0])
    assert res["status"] == "PASS"
    assert res["rule_violated"] is None
    assert abs(res["deviation_sd"] - 0.2) < 1e-6


def test_westgard_1_3s() -> None:
    eng = WestgardEngine()
    # 3.4 SD above the mean -> 1-3S reject.
    res = eng.evaluate(11.7, target=10.0, sd=0.5, history=[10.0, 10.1, 9.9])
    assert res["status"] == "FAIL"
    assert res["rule_violated"] == "1-3S"


def test_westgard_2_2s() -> None:
    eng = WestgardEngine()
    # Previous point at +2.2 SD, current at +2.4 SD -> two consecutive > +2SD.
    # history's last value is the "previous" point.
    res = eng.evaluate(11.2, target=10.0, sd=0.5, history=[10.0, 11.1])
    assert res["status"] == "FAIL"
    assert res["rule_violated"] == "2-2S"


def test_westgard_10x() -> None:
    eng = WestgardEngine()
    # Nine prior points + current all above the mean -> 10x.
    history = [10.2, 10.1, 10.3, 10.2, 10.4, 10.1, 10.2, 10.3, 10.1]
    res = eng.evaluate(10.2, target=10.0, sd=0.5, history=history)
    assert res["status"] == "FAIL"
    assert res["rule_violated"] == "10x"


def test_westgard_r_4s_opposite_sides() -> None:
    eng = WestgardEngine()
    # Prior point at +2.5 SD, current at -2.5 SD -> opposite sides -> R-4S FAIL.
    res = eng.evaluate(95.0, target=100.0, sd=2.0, history=[105.0])
    assert res["status"] == "FAIL"
    assert res["rule_violated"] == "R-4S"


def test_westgard_r_4s_same_side_does_not_fire() -> None:
    eng = WestgardEngine()
    # Prior +3.5 SD, current -0.6 SD: |range|>=4 but NOT opposite sides each
    # beyond 2 SD -> the old range-only check wrongly flagged R-4S; now PASS.
    res = eng.evaluate(98.8, target=100.0, sd=2.0, history=[107.0])
    assert res["rule_violated"] != "R-4S"


def test_westgard_calculate_stats() -> None:
    eng = WestgardEngine()
    stats = eng.calculate_stats([10.0, 12.0, 14.0])
    assert abs(stats["mean"] - 12.0) < 1e-9
    assert stats["sd"] > 0
    assert stats["cv_percent"] > 0


# --------------------------------------------------------------------------- #
# QConnect
# --------------------------------------------------------------------------- #
def test_qconnect_pass_within_limits() -> None:
    eng = QConnectEngine()
    limits = {"lcl": 0.8, "ucl": 1.2, "history": [0.9, 1.0, 1.1, 1.05, 0.95]}
    res = eng.evaluate(1.0, limits)
    assert res["status"] == "PASS"
    assert res["lcl"] == 0.8
    assert res["ucl"] == 1.2
    assert 0.0 <= res["percentile_position"] <= 1.0


def test_qconnect_fail_outside_limits() -> None:
    eng = QConnectEngine()
    limits = {"lcl": 0.8, "ucl": 1.2, "history": [0.9, 1.0, 1.1]}
    res = eng.evaluate(1.5, limits)
    assert res["status"] == "FAIL"


def test_qconnect_derives_limits_from_history() -> None:
    eng = QConnectEngine()
    # No explicit lcl/ucl: derive from percentiles of history.
    history = [float(x) for x in range(1, 101)]  # 1..100
    res = eng.evaluate(50.0, {"history": history})
    assert res["status"] == "PASS"
    assert res["percentile_5"] < res["percentile_95"]


def test_qconnect_compare_with_westgard_agreement() -> None:
    eng = QConnectEngine()
    cmp = eng.compare_with_westgard({"status": "PASS"}, {"status": "PASS"})
    assert cmp["agreement"] is True
    cmp2 = eng.compare_with_westgard({"status": "PASS"}, {"status": "FAIL"})
    assert cmp2["agreement"] is False
    assert cmp2["combined_status"] == "REVIEW_REQUIRED"


# --------------------------------------------------------------------------- #
# Anomaly detection
# --------------------------------------------------------------------------- #
def test_anomaly_flags_outlier() -> None:
    det = AnomalyDetector()
    history = [10.0, 10.1, 9.9, 10.0, 10.2, 9.8, 10.1, 10.0]
    res = det.detect(25.0, history)  # gross outlier
    assert res["anomaly_detected"] is True
    assert res["anomaly_score"] > 0.5


def test_anomaly_passes_normal_point() -> None:
    det = AnomalyDetector()
    history = [10.0, 10.1, 9.9, 10.0, 10.2, 9.8, 10.1, 10.0]
    res = det.detect(10.05, history)
    assert res["anomaly_detected"] is False


def test_anomaly_abstains_with_short_history() -> None:
    det = AnomalyDetector()
    res = det.detect(10.0, [10.0])
    assert res["anomaly_detected"] is False
    assert res["anomaly_score"] == 0.0


# --------------------------------------------------------------------------- #
# LSTM-lite fallback
# --------------------------------------------------------------------------- #
def test_lstm_fallback_returns_probability() -> None:
    lstm = LSTMLite()
    # No model loaded -> heuristic fallback must still produce a probability.
    assert lstm.available is False
    res = lstm.predict([10.0, 10.1, 10.2, 10.3, 10.4, 10.5])
    assert 0.0 <= res["failure_probability_48h"] <= 1.0
    assert res["model_used"] == "heuristic"
    assert len(res["timeline_hours"]) > 0
    # A clear upward drift should yield a non-trivial probability.
    assert res["failure_probability_48h"] > 0.0


def test_lstm_load_missing_model_degrades() -> None:
    lstm = LSTMLite()
    ok = lstm.load_model("/nonexistent/model.tflite")
    assert ok is False
    assert lstm.available is False
    # Still predicts via fallback.
    res = lstm.predict([1.0, 1.0, 1.0])
    assert "failure_probability_48h" in res


# --------------------------------------------------------------------------- #
# EdgeCache roundtrip
# --------------------------------------------------------------------------- #
def test_edge_cache_roundtrip(tmp_path) -> None:
    db_path = os.path.join(str(tmp_path), "test_cache.db")
    with EdgeCache(db_path) as cache:
        qc_data = {
            "lab_id": "lab-1",
            "analyzer_id": "AN-1",
            "analyte_code": "HCV-AB",
            "qc_lot_id": "LOT-1",
            "result_value": 1.45,
            "timestamp": "2026-03-15T09:30:00+00:00",
        }
        result = {"qc_status": "PASS", "severity": "LOW", "confidence": 0.85}

        row_id = cache.save_qc_result(qc_data, result)
        assert row_id > 0

        # Pending uploads should contain our record.
        pending = cache.get_pending_uploads()
        assert len(pending) == 1
        assert pending[0]["analyte_code"] == "HCV-AB"
        assert pending[0]["evaluation_result"]["qc_status"] == "PASS"
        assert cache.count_pending_uploads() == 1

        # Mark uploaded -> no longer pending.
        updated = cache.mark_uploaded([row_id])
        assert updated == 1
        assert cache.count_pending_uploads() == 0


def test_save_qc_result_embeds_full_qc_input(tmp_path) -> None:
    """The real save path must persist the full QC input so the cloud-sync
    uploader can reconstruct a faithful QCDataInput (correct target/sd/type),
    not lossy defaults. Regression guard for the edge->cloud sync contract.
    """
    db_path = os.path.join(str(tmp_path), "embed.db")
    with EdgeCache(db_path) as cache:
        qc_data = {
            "lab_id": "lab-1",
            "analyzer_id": "AN-1",
            "analyte_code": "HCV-AB",
            "analyte_type": "serology",
            "qc_lot_id": "LOT-1",
            "qc_level": "NORMAL",
            "result_value": 1.45,
            "target_value": 1.50,
            "sd_value": 0.08,
            "operator_id": "EMP1",
            "timestamp": "2026-03-15T09:30:00+00:00",
        }
        result = {"qc_status": "PASS", "severity": "LOW", "confidence": 0.9}
        cache.save_qc_result(qc_data, result)

        rec = cache.get_pending_uploads()[0]["evaluation_result"]
        # Response keys are still present at the top level (additive embed).
        assert rec["qc_status"] == "PASS"
        # The full input is embedded with the real (non-default) statistics.
        embedded = rec["qc_input"]
        assert embedded["target_value"] == 1.50
        assert embedded["sd_value"] == 0.08
        assert embedded["analyte_type"] == "serology"
        assert embedded["operator_id"] == "EMP1"


def test_edge_cache_history_and_summary(tmp_path) -> None:
    db_path = os.path.join(str(tmp_path), "hist.db")
    with EdgeCache(db_path) as cache:
        for i, v in enumerate([1.0, 1.1, 1.2]):
            cache.save_qc_result(
                {
                    "lab_id": "lab-1",
                    "analyzer_id": "AN-1",
                    "analyte_code": "HCV-AB",
                    "qc_lot_id": "LOT-1",
                    "result_value": v,
                    "timestamp": f"2026-03-15T09:3{i}:00+00:00",
                },
                {"qc_status": "PASS"},
            )
        history = cache.get_local_qc_history("HCV-AB", days=3650)
        assert history == [1.0, 1.1, 1.2]

        summary = cache.get_qc_summary_24h()
        # Records are dated in 2026; relative to "now" they may fall outside 24h,
        # so just assert the call works and returns a list.
        assert isinstance(summary, list)


def test_edge_cache_control_limits_roundtrip(tmp_path) -> None:
    db_path = os.path.join(str(tmp_path), "limits.db")
    with EdgeCache(db_path) as cache:
        cache.save_control_limits("HCV-AB", {"lcl": 0.8, "ucl": 1.2})
        limits = cache.get_control_limits("HCV-AB")
        assert limits is not None
        assert limits["lcl"] == 0.8
        # A zero TTL forces the entry to read as stale.
        stale = cache.get_control_limits("HCV-AB", ttl_seconds=0)
        assert stale is None
        # Unknown analyte returns None.
        assert cache.get_control_limits("UNKNOWN") is None


def test_edge_cache_model_roundtrip(tmp_path) -> None:
    db_path = os.path.join(str(tmp_path), "models.db")
    with EdgeCache(db_path) as cache:
        cache.save_model("lstm_lite", b"\x00\x01\x02", "1.0.0")
        rec = cache.get_model("lstm_lite")
        assert rec is not None
        assert rec["version"] == "1.0.0"
        assert rec["binary"] == b"\x00\x01\x02"
        assert any(m["model_name"] == "lstm_lite" for m in cache.list_models())


def test_edge_cache_hl7_raw(tmp_path) -> None:
    db_path = os.path.join(str(tmp_path), "hl7.db")
    with EdgeCache(db_path) as cache:
        rid = cache.save_hl7_raw("MSH|^~\\&|...", "HCV-AB")
        assert rid > 0
        assert cache.ping() is True
