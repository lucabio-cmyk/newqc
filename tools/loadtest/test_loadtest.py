"""Unit tests for the load-harness pure helpers (no network)."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from qc_load import LoadResult, build_payload, percentile  # noqa: E402


def test_build_payload_deterministic() -> None:
    a = build_payload(7, 3)
    b = build_payload(7, 3)
    assert a == b  # same (seed, index) -> identical payload
    assert build_payload(7, 4) != a


def test_build_payload_shape() -> None:
    p = build_payload(1, 0)
    required = {
        "lab_id",
        "analyzer_id",
        "analyte_code",
        "analyte_type",
        "qc_lot_id",
        "qc_level",
        "result_value",
        "target_value",
        "sd_value",
        "operator_id",
    }
    assert required <= set(p)
    assert p["result_value"] > 0


def test_build_payload_injects_outliers() -> None:
    # index % 11 == 0 forces a >3 SD deviation; index 0 is such a case.
    outlier = build_payload(1, 0)
    dev = abs(outlier["result_value"] - outlier["target_value"]) / outlier["sd_value"]
    assert dev > 3.0


def test_percentile() -> None:
    data = [float(i) for i in range(1, 101)]  # 1..100
    assert percentile(data, 50) == 50.5
    assert percentile([], 95) == 0.0
    assert percentile([42.0], 99) == 42.0
    assert percentile(data, 100) == 100.0


def test_load_result_summary() -> None:
    r = LoadResult(total=100, errors=2, latencies_ms=[float(i) for i in range(1, 101)])
    r.duration_s = 10.0
    s = r.summary()
    assert s["requests"] == 100
    assert s["ok"] == 98
    assert s["error_rate"] == 0.02
    assert s["throughput_rps"] == 9.8
    assert s["latency_ms_p50"] == 50.5
    assert s["latency_ms_max"] == 100.0
