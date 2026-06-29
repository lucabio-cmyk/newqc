"""HL7 v2.x ingestion for the edge node.

Analyzers in the lab speak HL7 v2 over MLLP (Minimal Lower Layer Protocol).
This package provides:

* :class:`~edge.qc_inference.hl7.parser.HL7Parser` - parse MSH/OBX, build ACKs.
* :class:`~edge.qc_inference.hl7.validator` - cheap structural validation.
* :class:`~edge.qc_inference.hl7.listener.MLLPListener` - async MLLP TCP server.
"""

from __future__ import annotations
