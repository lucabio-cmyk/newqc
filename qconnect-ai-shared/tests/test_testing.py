"""Tests for the deterministic factories in :mod:`shared.testing`.

These keep the shared package self-contained: the HL7 message is asserted
structurally (MSH/OBX segments) rather than round-tripped through the edge
parser, which is exercised in the cross-project integration suite instead.
"""

from __future__ import annotations

import pytest

from shared.models import AnalyteType, QCDataInput
from shared.testing import (
    make_control_limits,
    make_failing_qc_input,
    make_hl7_message,
    make_qc_history,
    make_qc_input,
)


# --------------------------------------------------------------------------- #
# QC input factories
# --------------------------------------------------------------------------- #
def test_make_qc_input_defaults() -> None:
    qc = make_qc_input()
    assert isinstance(qc, QCDataInput)
    assert qc.analyte_code == "HCV-AB"
    assert qc.analyte_type == AnalyteType.SEROLOGY
    assert qc.timestamp.tzinfo is not None


def test_make_qc_input_overrides() -> None:
    qc = make_qc_input(analyte_code="HBSAG", result_value=0.2, lab_id="lab-x")
    assert qc.analyte_code == "HBSAG"
    assert qc.result_value == 0.2
    assert qc.lab_id == "lab-x"


def test_make_qc_input_validates() -> None:
    # result_value must be > 0; the factory must not silently accept bad input.
    with pytest.raises(Exception):
        make_qc_input(result_value=0)


def test_make_failing_qc_input_exceeds_3sd() -> None:
    qc = make_failing_qc_input()
    deviation_sd = (qc.result_value - qc.target_value) / qc.sd_value
    assert deviation_sd > 3.0


def test_make_failing_qc_input_respects_overrides() -> None:
    qc = make_failing_qc_input(target_value=10.0, sd_value=1.0)
    deviation_sd = (qc.result_value - qc.target_value) / qc.sd_value
    assert deviation_sd > 3.0
    assert qc.target_value == 10.0


# --------------------------------------------------------------------------- #
# History generation
# --------------------------------------------------------------------------- #
def test_history_is_deterministic_for_seed() -> None:
    a = make_qc_history(1.5, 0.08, n=30, seed=42)
    b = make_qc_history(1.5, 0.08, n=30, seed=42)
    assert a == b
    assert len(a) == 30


def test_history_differs_across_seeds() -> None:
    a = make_qc_history(1.5, 0.08, n=30, seed=1)
    b = make_qc_history(1.5, 0.08, n=30, seed=2)
    assert a != b


def test_history_rejects_bad_args() -> None:
    with pytest.raises(ValueError):
        make_qc_history(1.0, 0.1, n=0)
    with pytest.raises(ValueError):
        make_qc_history(1.0, 0.1, distribution="nope")


def test_gaussian_history_roughly_centred() -> None:
    data = make_qc_history(100.0, 5.0, n=500, seed=7, distribution="gaussian")
    sample_mean = sum(data) / len(data)
    assert abs(sample_mean - 100.0) < 2.0  # within tolerance for n=500


def test_bimodal_history_has_two_clusters() -> None:
    mean, sd = 100.0, 5.0
    data = make_qc_history(mean, sd, n=400, seed=11, distribution="bimodal")
    # Points should separate into a low cluster and a high cluster around the
    # +/-2 SD offsets, leaving a sparse gap near the centre.
    low = [x for x in data if x < mean - sd]
    high = [x for x in data if x > mean + sd]
    near_centre = [x for x in data if abs(x - mean) <= sd * 0.5]
    assert len(low) > 50
    assert len(high) > 50
    # The central gap should be much sparser than either cluster.
    assert len(near_centre) < len(low)
    assert len(near_centre) < len(high)


def test_skewed_history_is_right_skewed() -> None:
    data = make_qc_history(1.0, 0.3, n=600, seed=3, distribution="skewed")
    n = len(data)
    mu = sum(data) / n
    var = sum((x - mu) ** 2 for x in data) / n
    sd = var**0.5
    skew = sum(((x - mu) / sd) ** 3 for x in data) / n
    assert skew > 0.3  # positive (right) skew


def test_lognormal_history_is_positive() -> None:
    data = make_qc_history(1.0, 0.25, n=200, seed=5, distribution="lognormal")
    assert all(x > 0 for x in data)


# --------------------------------------------------------------------------- #
# Control limits
# --------------------------------------------------------------------------- #
def test_make_control_limits_shape() -> None:
    history = make_qc_history(1.5, 0.08, n=50, seed=42)
    limits = make_control_limits(history)
    assert set(limits) >= {"history", "percentile_5", "percentile_95", "lcl", "ucl"}
    assert limits["percentile_5"] < limits["percentile_95"]
    assert limits["lcl"] == limits["percentile_5"]
    assert limits["ucl"] == limits["percentile_95"]
    assert limits["history"] == history


# --------------------------------------------------------------------------- #
# HL7 (structural assertions only; parser round-trip lives in integration tests)
# --------------------------------------------------------------------------- #
def test_make_hl7_message_structure() -> None:
    msg = make_hl7_message()
    segments = msg.split("\r")
    assert segments[0].startswith("MSH|^~\\&|")
    assert "ORU^R01" in segments[0]
    assert "MSG00001" in segments[0]
    assert any(s.startswith("OBX|") for s in segments)
    obx = next(s for s in segments if s.startswith("OBX|"))
    assert "HCV-AB" in obx
    assert "1.45" in obx


def test_make_hl7_message_overrides() -> None:
    msg = make_hl7_message(analyte="HBSAG", value=0.32, control_id="MSG99999")
    assert "MSG99999" in msg
    assert "HBSAG" in msg
    assert "0.32" in msg
