"""Cheap structural validation for inbound HL7 messages.

Run before full parsing to reject obvious garbage early (and to log *why* a
message was rejected). This is intentionally lightweight - it checks framing and
the presence of a well-formed MSH header, not full conformance to a message
profile.
"""

from __future__ import annotations

from loguru import logger

# MLLP framing bytes.
_VT = "\x0b"
_FS = "\x1c"


def validate_message(raw: str) -> tuple[bool, list[str]]:
    """Validate the basic structure of an HL7 message.

    Checks performed:
        * MLLP framing is *optional*; if present, both start (VT) and end (FS)
          bytes should appear.
        * An MSH segment must be present and start the message.
        * MSH must declare a field separator and encoding characters.
        * MSH must contain enough fields to carry a message type and control id.
        * The declared separators must be used consistently (field separator
          appears in MSH).

    Args:
        raw: the raw message (framed or unframed).

    Returns:
        ``(is_valid, reasons)`` where ``reasons`` lists any problems found.
        ``reasons`` is empty when ``is_valid`` is True.
    """
    reasons: list[str] = []

    if not raw or not raw.strip():
        return False, ["empty message"]

    # --- Optional MLLP framing consistency. ---------------------------- #
    has_vt = _VT in raw
    has_fs = _FS in raw
    if has_vt != has_fs:
        reasons.append("incomplete MLLP framing (VT/FS mismatch)")

    # Strip framing for the structural checks.
    text = raw.replace(_VT, "").replace(_FS, "").strip("\r\n ")
    segments = [s for s in text.replace("\r\n", "\r").replace("\n", "\r").split("\r") if s.strip()]
    if not segments:
        reasons.append("no segments after stripping framing")
        return False, reasons

    # --- MSH must be the first segment. -------------------------------- #
    msh = segments[0]
    if not msh.startswith("MSH"):
        reasons.append("first segment is not MSH")
        return False, reasons

    if len(msh) < 8:
        reasons.append("MSH too short to declare separators")
        return False, reasons

    field_sep = msh[3]
    if field_sep.isalnum():
        reasons.append("MSH field separator looks invalid")

    # Encoding characters (MSH-2) should be present right after the field sep.
    fields = msh.split(field_sep)
    if len(fields) < 12:
        reasons.append(f"MSH has too few fields ({len(fields)}; expected >= 12)")

    if len(fields) > 1 and not fields[1]:
        reasons.append("MSH-2 encoding characters missing")

    # Message control id (MSH-10) and message type (MSH-9) should be non-empty.
    if len(fields) > 9 and not fields[9].strip():
        reasons.append("MSH-10 message control id missing")
    if len(fields) > 8 and not fields[8].strip():
        reasons.append("MSH-9 message type missing")

    is_valid = len(reasons) == 0
    if not is_valid:
        logger.debug("HL7 validation failed: {}", reasons)
    return is_valid, reasons
