"""HL7 v2.5 message parser and ACK builder (pure-Python, no external libs).

Clinical analyzers emit results as HL7 v2 messages. For QC ingestion we care
about two segment types:

* **MSH** - message header: timestamp, sender, message type, control id, and the
  encoding characters that define the field/component/repetition/escape/sub-
  component separators.
* **OBX** - observation/result: the analyte (observation identifier, often with a
  LOINC code), the measured value, units and the reference range.

The parser reads the encoding characters straight out of MSH so it adapts to any
analyzer's separators rather than assuming the defaults. It also exposes
:meth:`build_ack` to acknowledge received messages.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from loguru import logger

# Small illustrative LOINC lookup so locally coded analytes can be normalised.
# Mirrors the shared package's ``LOINC_MAP`` subset.
LOINC_MAP: dict[str, str] = {
    "HIV-AB": "75622-1",
    "HCV-AB": "13955-0",
    "HBSAG": "5196-1",
    "SYPHILIS": "20507-0",
    "TROPONIN-I": "10839-9",
    "TROPONIN-T": "6598-7",
}

# Default HL7 separators (used as a fallback if MSH does not declare them).
DEFAULT_FIELD_SEP = "|"
DEFAULT_COMPONENT_SEP = "^"
DEFAULT_REPETITION_SEP = "~"
DEFAULT_ESCAPE_CHAR = "\\"
DEFAULT_SUBCOMPONENT_SEP = "&"


class HL7Parser:
    """Parse HL7 v2.5 messages into plain dicts and build ACK responses."""

    def parse_message(self, raw: str) -> dict[str, Any]:
        """Parse a raw HL7 message string into a structured dict.

        MLLP framing bytes (VT/FS/CR), if present, are stripped first. Segments
        are split on ``\\r`` (HL7's canonical segment terminator), tolerating
        ``\\n`` line endings as well.

        Args:
            raw: the raw HL7 message (with or without MLLP framing).

        Returns:
            Dict with keys ``msh`` (header fields), ``observations`` (list of OBX
            dicts) and convenience top-level fields such as ``message_control_id``
            and ``analyte_code`` (first observation).

        Raises:
            ValueError: if no MSH segment can be found.
        """
        text = self._strip_mllp(raw)
        # HL7 segments are CR-terminated; be lenient about CRLF / LF.
        segments = [
            s for s in text.replace("\r\n", "\r").replace("\n", "\r").split("\r") if s.strip()
        ]
        if not segments:
            raise ValueError("Empty HL7 message")

        seps = self._encoding_chars(segments[0])

        msh = self._parse_msh(segments[0], seps)
        observations: list[dict[str, Any]] = []
        for seg in segments[1:]:
            if seg.startswith("OBX"):
                obx = self._parse_obx(seg, seps)
                if obx:
                    observations.append(obx)

        first = observations[0] if observations else {}
        parsed: dict[str, Any] = {
            "msh": msh,
            "message_control_id": msh.get("message_control_id"),
            "message_type": msh.get("message_type"),
            "sender": msh.get("sender"),
            "timestamp": msh.get("timestamp"),
            "observations": observations,
            "analyte_code": first.get("analyte_code"),
            "result_value": first.get("result_value"),
            "units": first.get("units"),
            "separators": seps,
        }
        logger.debug(
            "HL7Parser: parsed msg ctrl_id={} obs={}",
            parsed["message_control_id"],
            len(observations),
        )
        return parsed

    # ------------------------------------------------------------------ #
    # ACK
    # ------------------------------------------------------------------ #
    def build_ack(self, message_control_id: str | None, code: str = "AA") -> str:
        """Build an HL7 ACK message for a received message.

        Args:
            message_control_id: the MSH-10 of the message being acknowledged
                (echoed into MSA-2).
            code: acknowledgement code - ``AA`` (accept), ``AE`` (error),
                ``AR`` (reject).

        Returns:
            The ACK as a string using ``\\r`` segment terminators. Wrap it with
            :func:`frame_mllp` before sending over the wire.
        """
        now = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        ctrl = message_control_id or "UNKNOWN"
        # MSH-1 is the field separator; MSH-2 carries the encoding characters.
        msh = f"MSH|^~\\&|QCONNECT-EDGE|LAB|ANALYZER|LAB|{now}||ACK^R01|" f"{ctrl}|P|2.5"
        msa = f"MSA|{code}|{ctrl}"
        return msh + "\r" + msa + "\r"

    # ------------------------------------------------------------------ #
    # Internal parsing helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _strip_mllp(raw: str) -> str:
        """Remove MLLP framing bytes (VT 0x0b start, FS 0x1c, CR 0x0d end)."""
        return raw.replace("\x0b", "").replace("\x1c", "").strip("\r\n ")

    @staticmethod
    def _encoding_chars(msh_segment: str) -> dict[str, str]:
        """Extract the separators from an MSH segment.

        MSH-1 (the 4th character of the segment) is the field separator; the
        next field (MSH-2) declares the component/repetition/escape/sub-component
        characters, conventionally ``^~\\&``.
        """
        if len(msh_segment) < 4 or not msh_segment.startswith("MSH"):
            return {
                "field": DEFAULT_FIELD_SEP,
                "component": DEFAULT_COMPONENT_SEP,
                "repetition": DEFAULT_REPETITION_SEP,
                "escape": DEFAULT_ESCAPE_CHAR,
                "subcomponent": DEFAULT_SUBCOMPONENT_SEP,
            }
        field = msh_segment[3]
        enc = msh_segment[4:].split(field, 1)[0] if field in msh_segment[4:] else msh_segment[4:7]
        component = enc[0] if len(enc) > 0 else DEFAULT_COMPONENT_SEP
        repetition = enc[1] if len(enc) > 1 else DEFAULT_REPETITION_SEP
        escape = enc[2] if len(enc) > 2 else DEFAULT_ESCAPE_CHAR
        subcomponent = enc[3] if len(enc) > 3 else DEFAULT_SUBCOMPONENT_SEP
        return {
            "field": field,
            "component": component,
            "repetition": repetition,
            "escape": escape,
            "subcomponent": subcomponent,
        }

    def _parse_msh(self, segment: str, seps: dict[str, str]) -> dict[str, Any]:
        """Parse an MSH segment into named header fields.

        Note: MSH is special - the field separator itself occupies MSH-1, which
        shifts subsequent field indices by one relative to other segments.
        """
        field_sep = seps["field"]
        comp_sep = seps["component"]
        fields = segment.split(field_sep)
        # fields[0] = "MSH"; fields[1] = encoding chars (MSH-2);
        # MSH-3 sending app ... MSH-7 datetime ... MSH-9 type ... MSH-10 ctrl id.
        sender = self._get(fields, 2)
        msg_type_raw = self._get(fields, 8)
        message_type = msg_type_raw.replace(comp_sep, "^") if msg_type_raw else None
        return {
            "sending_application": self._get(fields, 2),
            "sending_facility": self._get(fields, 3),
            "receiving_application": self._get(fields, 4),
            "receiving_facility": self._get(fields, 5),
            "timestamp": self._get(fields, 6),
            "message_type": message_type,
            "message_control_id": self._get(fields, 9),
            "processing_id": self._get(fields, 10),
            "version_id": self._get(fields, 11),
            "sender": sender,
        }

    def _parse_obx(self, segment: str, seps: dict[str, str]) -> dict[str, Any] | None:
        """Parse an OBX (observation) segment.

        OBX layout (selected fields):
            OBX-3 observation identifier (id ^ text ^ coding-system),
            OBX-5 observation value,
            OBX-6 units,
            OBX-7 reference range.
        """
        field_sep = seps["field"]
        comp_sep = seps["component"]
        fields = segment.split(field_sep)
        if len(fields) < 4:
            return None

        obs_id_raw = self._get(fields, 3)
        comps = obs_id_raw.split(comp_sep) if obs_id_raw else []
        identifier = comps[0] if comps else None
        text = comps[1] if len(comps) > 1 else None
        coding_system = comps[2] if len(comps) > 2 else None

        analyte_code = identifier
        # If the coding system is LOINC, capture it; otherwise try our local map.
        loinc: str | None = None
        if coding_system and "LN" in coding_system.upper():
            loinc = identifier
        elif identifier and identifier.upper() in LOINC_MAP:
            loinc = LOINC_MAP[identifier.upper()]

        result_raw = self._get(fields, 5)
        return {
            "value_type": self._get(fields, 2),
            "analyte_code": analyte_code,
            "analyte_text": text,
            "coding_system": coding_system,
            "loinc": loinc,
            "result_value": self._to_float(result_raw),
            "result_raw": result_raw,
            "units": self._get(fields, 6),
            "reference_range": self._get(fields, 7),
            "abnormal_flags": self._get(fields, 8),
        }

    @staticmethod
    def _get(fields: list[str], idx: int) -> str | None:
        """Safe positional access into a split segment (empty -> ``None``)."""
        if 0 <= idx < len(fields):
            val = fields[idx].strip()
            return val or None
        return None

    @staticmethod
    def _to_float(value: str | None) -> float | None:
        """Best-effort numeric coercion of an OBX result value."""
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


# --------------------------------------------------------------------------- #
# MLLP framing helpers
# --------------------------------------------------------------------------- #
MLLP_START = b"\x0b"  # VT
MLLP_END = b"\x1c\x0d"  # FS + CR


def frame_mllp(message: str) -> bytes:
    """Wrap an HL7 message string in MLLP framing bytes ready to send."""
    return MLLP_START + message.encode("utf-8") + MLLP_END
