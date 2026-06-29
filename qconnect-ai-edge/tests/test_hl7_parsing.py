"""Tests for HL7 v2.5 parsing, ACK building and validation.

These exercise only the pure-Python HL7 modules - no FastAPI import.
"""

from __future__ import annotations

from edge.qc_inference.hl7.parser import HL7Parser, frame_mllp
from edge.qc_inference.hl7.validator import validate_message

# A realistic HL7 v2.5 ORU^R01 message carrying two OBX results.
# Segments are CR-separated as per the standard.
SAMPLE_HL7 = "\r".join(
    [
        "MSH|^~\\&|ARCHITECT|LAB|QCONNECT|LAB|20260315093000||ORU^R01|MSG00001|P|2.5",
        "PID|1||PATID1234^^^LAB^MR||DOE^JOHN",
        "OBR|1||ORD123|HCV^Anti-HCV^L",
        "OBX|1|NM|HCV-AB^Anti-HCV^LN||1.45|S/CO|0.00-1.00|N|||F",
        "OBX|2|NM|HBSAG^HBsAg^LN||0.32|S/CO|0.00-1.00|N|||F",
    ]
)


def test_parse_message_extracts_msh() -> None:
    parsed = HL7Parser().parse_message(SAMPLE_HL7)
    assert parsed["message_control_id"] == "MSG00001"
    assert parsed["message_type"] == "ORU^R01"
    assert parsed["sender"] == "ARCHITECT"
    assert parsed["timestamp"] == "20260315093000"


def test_parse_message_extracts_obx() -> None:
    parsed = HL7Parser().parse_message(SAMPLE_HL7)
    obs = parsed["observations"]
    assert len(obs) == 2

    first = obs[0]
    assert first["analyte_code"] == "HCV-AB"
    assert first["result_value"] == 1.45
    assert first["units"] == "S/CO"
    assert first["reference_range"] == "0.00-1.00"
    assert first["loinc"] == "HCV-AB"  # coded as LN in OBX-3

    # Convenience top-level fields mirror the first observation.
    assert parsed["analyte_code"] == "HCV-AB"
    assert parsed["result_value"] == 1.45


def test_parse_message_with_mllp_framing() -> None:
    framed = frame_mllp(SAMPLE_HL7).decode("utf-8")
    parsed = HL7Parser().parse_message(framed)
    assert parsed["message_control_id"] == "MSG00001"
    assert parsed["analyte_code"] == "HCV-AB"


def test_build_ack_accept() -> None:
    ack = HL7Parser().build_ack("MSG00001", code="AA")
    assert "MSA|AA|MSG00001" in ack
    assert ack.startswith("MSH|^~\\&|")
    # Round-trips through the validator as a well-formed message.
    ok, reasons = validate_message(ack)
    assert ok, reasons


def test_build_ack_handles_missing_id() -> None:
    ack = HL7Parser().build_ack(None, code="AR")
    assert "MSA|AR|UNKNOWN" in ack


def test_validator_accepts_good_message() -> None:
    ok, reasons = validate_message(SAMPLE_HL7)
    assert ok, reasons
    assert reasons == []


def test_validator_rejects_non_msh() -> None:
    bad = "PID|1||x\rOBX|1|NM|HCV||1.0"
    ok, reasons = validate_message(bad)
    assert not ok
    assert any("MSH" in r for r in reasons)


def test_validator_rejects_empty() -> None:
    ok, reasons = validate_message("")
    assert not ok
    assert reasons


def test_validator_rejects_truncated_msh() -> None:
    # MSH present but far too few fields.
    ok, reasons = validate_message("MSH|^~\\&|ONLY|FEW|FIELDS")
    assert not ok
    assert reasons
