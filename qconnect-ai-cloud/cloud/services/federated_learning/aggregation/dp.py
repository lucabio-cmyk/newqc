"""Differential-privacy helpers for federated aggregation.

We use the **analytic Gaussian mechanism**: bound each client's contribution by
clipping its L2 norm to ``C`` (the sensitivity), then add zero-mean Gaussian
noise with standard deviation ``sigma`` to the aggregate. For the classic
Gaussian mechanism, ``(epsilon, delta)``-DP is guaranteed when::

    sigma >= C * sqrt(2 * ln(1.25 / delta)) / epsilon

Privacy / utility tradeoff
--------------------------
* Smaller ``epsilon`` (stronger privacy) -> larger ``sigma`` -> more noise ->
  lower model utility.
* Larger ``delta`` -> smaller ``sigma`` -> weaker privacy but higher utility.
  Keep ``delta`` well below ``1 / num_clients`` (often << 1e-5).
* Tighter clipping ``C`` reduces sensitivity and thus noise, but clips away
  genuine signal from large updates. ``C`` is the lever that trades how much of
  each update survives against how much noise is required.
"""

from __future__ import annotations

import math

try:  # numpy optional, used only for the (faster, vectorised) noise path
    import numpy as _np
except ImportError:  # pragma: no cover
    _np = None


def clip_l2(vector: list[float], max_norm: float) -> list[float]:
    """Clip ``vector`` so its L2 norm does not exceed ``max_norm``.

    Bounds the per-client sensitivity. If the norm is already within
    ``max_norm`` the vector is returned unchanged (as floats).
    """
    if max_norm <= 0:
        raise ValueError("max_norm must be positive")
    norm = math.sqrt(sum(float(x) * float(x) for x in vector))
    if norm <= max_norm or norm == 0.0:
        return [float(x) for x in vector]
    scale = max_norm / norm
    return [float(x) * scale for x in vector]


def add_gaussian_noise(vector: list[float], sigma: float, seed: int | None = None) -> list[float]:
    """Add zero-mean Gaussian noise with std ``sigma`` to ``vector``.

    Deterministic for a given ``seed`` (uses ``numpy.random.default_rng`` when
    numpy is available, otherwise ``random.Random``). ``sigma == 0`` returns the
    vector unchanged.
    """
    if sigma < 0:
        raise ValueError("sigma must be non-negative")
    if sigma == 0:
        return [float(x) for x in vector]

    if _np is not None:
        rng = _np.random.default_rng(seed)
        noise = rng.normal(0.0, sigma, size=len(vector))
        return [float(x) + float(n) for x, n in zip(vector, noise)]

    import random

    rng_py = random.Random(seed)
    return [float(x) + rng_py.gauss(0.0, sigma) for x in vector]


def gaussian_sigma_for(epsilon: float, delta: float, sensitivity: float) -> float:
    """Return the Gaussian-mechanism ``sigma`` for ``(epsilon, delta)``-DP.

    ``sigma = sensitivity * sqrt(2 * ln(1.25 / delta)) / epsilon``.

    A non-finite ``epsilon`` (no privacy requested) yields ``sigma == 0``.
    """
    if sensitivity < 0:
        raise ValueError("sensitivity must be non-negative")
    if not math.isfinite(epsilon):
        return 0.0
    if epsilon <= 0:
        raise ValueError("epsilon must be positive")
    if not 0.0 < delta < 1.0:
        raise ValueError("delta must be in (0, 1)")
    return sensitivity * math.sqrt(2.0 * math.log(1.25 / delta)) / epsilon
