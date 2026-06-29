"""Unit tests for the shared library."""

import pytest

from shared.constants import sigma_category
from shared.models import (
    AnalyteType,
    QCDataInput,
    QCEvaluationResponse,
    QCLevelType,
    QCStatusEnum,
    SeverityEnum,
    AIInsights,
)
from shared.security import (
    aes_decrypt,
    aes_encrypt,
    create_jwt,
    decode_jwt,
    generate_aes_key,
    sign_payload,
    verify_signature,
)
from shared.utils import cv_percent, empirical_cdf_position, mean, percentile, stddev, z_score


def _sample_input(**overrides) -> QCDataInput:
    data = dict(
        lab_id="lab-genova-001",
        analyzer_id="ABBOTT-ARCHITECT-001",
        analyte_code="HCV-AB",
        analyte_type=AnalyteType.SEROLOGY,
        qc_lot_id="QC-HCV-DIAMEX-202603-001",
        qc_level=QCLevelType.NORMAL,
        result_value=1.45,
        target_value=1.50,
        sd_value=0.08,
        operator_id="EMP00234",
    )
    data.update(overrides)
    return QCDataInput(**data)


def test_qc_data_input_roundtrip():
    qc = _sample_input()
    again = QCDataInput.model_validate_json(qc.model_dump_json())
    assert again.analyte_code == "HCV-AB"
    assert again.timestamp.tzinfo is not None  # validator coerces to tz-aware


@pytest.mark.parametrize("bad", [{"result_value": 0}, {"sd_value": 0}, {"lab_id": ""}])
def test_qc_data_input_validation(bad):
    with pytest.raises(Exception):
        _sample_input(**bad)


def test_evaluation_response():
    resp = QCEvaluationResponse(
        qc_status=QCStatusEnum.PASS,
        severity=SeverityEnum.LOW,
        ai_insights=AIInsights(distribution_type="gaussian"),
        confidence=0.92,
    )
    assert 0 <= resp.confidence <= 1
    assert resp.evaluated_offline is False


@pytest.mark.parametrize(
    "sigma,label",
    [(6.5, ">6"), (5.0, "4-6"), (3.5, "3-4"), (2.5, "2-3"), (1.0, "<2")],
)
def test_sigma_category(sigma, label):
    assert sigma_category(sigma)[0] == label


def test_stats_helpers():
    data = [10.0, 12.0, 14.0, 16.0, 18.0]
    assert mean(data) == 14.0
    assert stddev(data) == pytest.approx(3.1623, rel=1e-3)
    assert cv_percent(data) == pytest.approx(22.59, rel=1e-2)
    assert z_score(16.0, 14.0, 2.0) == 1.0
    assert percentile(data, 50) == 14.0
    assert empirical_cdf_position(14.0, data) == pytest.approx(0.6)


def test_jwt_roundtrip():
    token = create_jwt("lab-genova-001", "secret", extra_claims={"role": "lab"})
    claims = decode_jwt(token, "secret")
    assert claims["sub"] == "lab-genova-001"
    assert claims["role"] == "lab"


def test_aes_roundtrip():
    key = generate_aes_key()
    ct = aes_encrypt(b"sensitive qc record", key)
    assert aes_decrypt(ct, key) == b"sensitive qc record"


def test_signature():
    sig = sign_payload(b"payload", "secret")
    assert verify_signature(b"payload", sig, "secret")
    assert not verify_signature(b"tampered", sig, "secret")
