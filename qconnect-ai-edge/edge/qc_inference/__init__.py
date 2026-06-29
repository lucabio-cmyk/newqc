"""QConnect-AI EDGE inference service.

The per-lab FastAPI application that ingests QC results (HTTP or HL7/MLLP),
evaluates them locally with the Westgard / QConnect / LSTM-lite / anomaly
engines and caches the outcome for later cloud sync. Designed to run fully
offline inside the laboratory.
"""

from __future__ import annotations

__version__ = "0.1.0"
