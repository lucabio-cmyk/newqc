"""Aggregation primitives for federated learning.

Provides FedAvg weight aggregation (:mod:`fedavg`) and differential-privacy
helpers (:mod:`dp`). All functions operate on plain ``list[float]`` vectors so
that callers do not need numpy, although numpy is used transparently when
available for speed.
"""

from __future__ import annotations

from .dp import add_gaussian_noise, clip_l2, gaussian_sigma_for
from .fedavg import apply_update, federated_average, model_delta_norm

__all__ = [
    "add_gaussian_noise",
    "apply_update",
    "clip_l2",
    "federated_average",
    "gaussian_sigma_for",
    "model_delta_norm",
]
