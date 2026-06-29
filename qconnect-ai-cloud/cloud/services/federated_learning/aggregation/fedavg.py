"""FedAvg aggregation primitives.

Implements the classic *Federated Averaging* (McMahan et al., 2017) reduction:
the global model update is the mean of client weight vectors weighted by the
number of local training samples each client used. Labs only ever transmit
weight vectors, never raw QC data.

All functions are numpy-optional: when numpy is importable it is used for speed,
otherwise a pure-Python fallback produces identical results.
"""

from __future__ import annotations

import math
from typing import Any

try:  # numpy is optional -- a pure-python path is always available.
    import numpy as _np
except ImportError:  # pragma: no cover - exercised only without numpy installed
    _np = None


def _validate_updates(updates: list[dict[str, Any]]) -> int:
    """Validate the shape of FedAvg updates and return the common vector length.

    Each update must be a mapping with a ``weights`` list and a positive integer
    ``num_samples``. All weight vectors must share the same length.
    """
    if not updates:
        raise ValueError("no updates to aggregate")

    dim: int | None = None
    for i, upd in enumerate(updates):
        weights = upd.get("weights")
        num_samples = upd.get("num_samples")
        if weights is None or num_samples is None:
            raise ValueError(f"update {i} missing 'weights' or 'num_samples'")
        if not isinstance(weights, (list, tuple)):
            raise ValueError(f"update {i} 'weights' must be a list")
        if len(weights) == 0:
            raise ValueError(f"update {i} 'weights' is empty")
        if not isinstance(num_samples, int) or num_samples <= 0:
            raise ValueError(f"update {i} 'num_samples' must be a positive int")
        if dim is None:
            dim = len(weights)
        elif len(weights) != dim:
            raise ValueError(f"update {i} has weight length {len(weights)}, expected {dim}")
    assert dim is not None  # guaranteed by the non-empty check above
    return dim


def federated_average(updates: list[dict[str, Any]]) -> list[float]:
    """Return the sample-weighted mean of client weight vectors (FedAvg).

    ``updates`` is a list of ``{"weights": list[float], "num_samples": int}``.
    The result is ``sum(n_k * w_k) / sum(n_k)`` computed element-wise.

    Raises ``ValueError`` if updates are empty, malformed, or have mismatched
    vector lengths.
    """
    dim = _validate_updates(updates)
    total = sum(int(u["num_samples"]) for u in updates)

    if _np is not None:
        mat = _np.array([u["weights"] for u in updates], dtype=float)
        counts = _np.array([u["num_samples"] for u in updates], dtype=float)
        weighted = (mat * counts[:, None]).sum(axis=0) / total
        return [float(x) for x in weighted]

    acc = [0.0] * dim
    for u in updates:
        n = int(u["num_samples"])
        for j, w in enumerate(u["weights"]):
            acc[j] += n * float(w)
    return [x / total for x in acc]


def model_delta_norm(a: list[float], b: list[float]) -> float:
    """Return the L2 norm of ``a - b`` (e.g. distance between two models)."""
    if len(a) != len(b):
        raise ValueError(f"length mismatch: {len(a)} vs {len(b)}")
    if _np is not None:
        return float(_np.linalg.norm(_np.asarray(a, dtype=float) - _np.asarray(b, dtype=float)))
    return math.sqrt(sum((float(x) - float(y)) ** 2 for x, y in zip(a, b)))


def apply_update(
    global_weights: list[float], aggregated: list[float], lr: float = 1.0
) -> list[float]:
    """Apply an aggregated update to the global model.

    Returns ``global_weights + lr * aggregated``. With ``lr=1.0`` and an
    aggregated vector that already represents new weights this is a server-side
    learning rate of 1; when ``aggregated`` is a delta, ``lr`` scales it.
    """
    if len(global_weights) != len(aggregated):
        raise ValueError(f"length mismatch: {len(global_weights)} vs {len(aggregated)}")
    if _np is not None:
        out = _np.asarray(global_weights, dtype=float) + lr * _np.asarray(aggregated, dtype=float)
        return [float(x) for x in out]
    return [float(g) + lr * float(a) for g, a in zip(global_weights, aggregated)]
