"""Local persistence layer for the edge node (SQLite, stdlib only).

Holds pending QC results awaiting cloud upload, cached control limits, cached
model blobs and raw HL7 messages so the node keeps working through network
outages. See :class:`edge.qc_inference.cache.sqlite_manager.EdgeCache`.
"""

from __future__ import annotations
