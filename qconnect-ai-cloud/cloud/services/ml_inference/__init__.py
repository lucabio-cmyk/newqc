"""ML-Inference service.

Serves AI enrichment for QC evaluation: 48h failure probability, anomaly score,
clinical-impact estimate and a recommended action. The production version would
load trained models (gradient-boosted failure predictor, isolation-forest
anomaly detector); this service returns transparent numpy-based heuristics (see
the ``models`` package) so the rest of the platform integrates against a real,
explainable, production-shaped endpoint.
"""

from __future__ import annotations

__version__ = "0.2.0"
