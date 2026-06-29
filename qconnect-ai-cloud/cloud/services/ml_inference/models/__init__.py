"""Heuristic ML models for the ML-inference service.

This package houses transparent, numpy-based (numpy-optional) stand-ins for the
production models that QConnect-AI will eventually serve:

* :class:`~models.failure_predictor.FailurePredictor` — 48h QC failure
  probability from z-score / trend / drift / variance features.
* :class:`~models.anomaly_detector.AnomalyDetector` — robust (median/MAD + IQR)
  outlier scoring, with an optional isolation-forest upgrade path.
* :class:`~models.explainer.Explainer` — lightweight additive (SHAP-like)
  attribution plus a synthesized recommended action and clinical-impact estimate.

None of these are trained models; they are deterministic heuristics so the rest
of the platform has a real, explainable endpoint to integrate against.
"""

from __future__ import annotations

from .anomaly_detector import AnomalyDetector
from .explainer import Explainer
from .failure_predictor import FailurePredictor

__all__ = ["AnomalyDetector", "Explainer", "FailurePredictor"]
