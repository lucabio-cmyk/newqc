"""QC-Evaluation service.

The orchestrator that fuses the legacy QC engines (Westgard, QConnect, Sigma,
distribution detection) with AI enrichment from the ML-inference service into a
single unified :class:`schemas.QCEvaluationResponse`.
"""

from __future__ import annotations

__version__ = "0.1.0"
