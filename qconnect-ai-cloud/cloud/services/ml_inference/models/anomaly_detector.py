"""Robust anomaly detector for QC control values (heuristic).

The default path uses two complementary, distribution-free signals over the
recent history:

* **Robust z-score** — ``|value - median| / (1.4826 * MAD)``. The MAD (median
  absolute deviation) scaled by ``1.4826`` is a consistent estimator of the
  standard deviation for normal data but is resistant to outliers, unlike the
  classical mean/SD z-score.
* **IQR fence** — Tukey's rule: a value outside ``[Q1 - 1.5*IQR, Q3 + 1.5*IQR]``
  is flagged. The distance beyond the fence (in IQR units) feeds the score.

The two signals are combined and squashed into an ``anomaly_score`` in roughly
``[0, 1]``; ``anomaly_detected`` trips when either signal crosses its threshold.

Future upgrade: an isolation forest (``sklearn.ensemble.IsolationForest``) gives
a multivariate, density-aware score. It is wired in *optionally* — used only when
scikit-learn imports successfully AND the history is long enough — but the
DEFAULT path never requires sklearn.
"""

from __future__ import annotations

import math

try:  # numpy is optional; pure-python fallbacks cover the same maths.
    import numpy as _np
except Exception:  # pragma: no cover - numpy is installed in this environment.
    _np = None


# Thresholds for the default (robust) path.
_ROBUST_Z_THRESHOLD = 3.5  # classic MAD-based outlier cutoff (Iglewicz & Hoaglin)
_IQR_K = 1.5  # Tukey fence multiplier
_MIN_HISTORY = 4  # below this, robust stats are unreliable
_ISOLATION_FOREST_MIN = 50  # only consider sklearn when history is sizeable


def _percentile(values: list[float], q: float) -> float:
    """Linear-interpolation percentile (``q`` in ``[0, 100]``)."""
    if not values:
        return 0.0
    if _np is not None:
        return float(_np.percentile(values, q))
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (q / 100.0) * (len(ordered) - 1)
    low = int(math.floor(rank))
    high = int(math.ceil(rank))
    if low == high:
        return ordered[low]
    frac = rank - low
    return ordered[low] * (1.0 - frac) + ordered[high] * frac


def _median(values: list[float]) -> float:
    """Median of ``values`` (``0.0`` for an empty sequence)."""
    return _percentile(values, 50.0)


class AnomalyDetector:
    """Distribution-free anomaly scorer for a single value against a history."""

    robust_z_threshold = _ROBUST_Z_THRESHOLD
    iqr_k = _IQR_K
    min_history = _MIN_HISTORY

    def _isolation_forest_score(self, value: float, history: list[float]) -> float | None:
        """Optional sklearn IsolationForest score, or ``None`` if unavailable.

        Returns a normalised anomaly score in ``[0, 1]`` (higher == more
        anomalous) when scikit-learn is importable and the history is long
        enough; otherwise ``None`` so the caller falls back to the robust path.
        """
        if len(history) < _ISOLATION_FOREST_MIN:
            return None
        try:  # pragma: no cover - sklearn is intentionally absent in tests.
            from sklearn.ensemble import IsolationForest
        except Exception:  # pragma: no cover
            return None
        try:  # pragma: no cover
            samples = [[v] for v in history]
            model = IsolationForest(random_state=0, n_estimators=100)
            model.fit(samples)
            # decision_function: higher == more normal. Flip and squash.
            raw = float(model.decision_function([[value]])[0])
            return max(0.0, min(1.0, 0.5 - raw))
        except Exception:  # pragma: no cover
            return None

    def score(self, value: float, history: list[float]) -> dict:
        """Score ``value`` for anomalousness against ``history``.

        Returns ``{anomaly_detected, anomaly_score, method}``.
        """
        value = float(value)
        hist = [float(x) for x in (history or [])]

        # Not enough history for robust statistics: be conservative.
        if len(hist) < self.min_history:
            return {
                "anomaly_detected": False,
                "anomaly_score": 0.0,
                "method": "insufficient_history",
            }

        # Optional isolation-forest upgrade (only when long history + sklearn).
        iso = self._isolation_forest_score(value, hist)
        if iso is not None:  # pragma: no cover - sklearn absent in tests.
            return {
                "anomaly_detected": iso >= 0.6,
                "anomaly_score": round(iso, 4),
                "method": "isolation_forest",
            }

        # --- Default path: robust z-score (median/MAD) + IQR fence. --------- #
        median = _median(hist)
        abs_dev = [abs(x - median) for x in hist]
        mad = _median(abs_dev)

        if mad > 0:
            robust_z = abs(value - median) / (1.4826 * mad)
        else:
            # Degenerate (all-identical) history: any deviation is a strong flag.
            robust_z = 0.0 if value == median else float("inf")

        q1 = _percentile(hist, 25.0)
        q3 = _percentile(hist, 75.0)
        iqr = q3 - q1
        lower = q1 - self.iqr_k * iqr
        upper = q3 + self.iqr_k * iqr
        if value < lower:
            iqr_excess = (lower - value) / iqr if iqr > 0 else float("inf")
        elif value > upper:
            iqr_excess = (value - upper) / iqr if iqr > 0 else float("inf")
        else:
            iqr_excess = 0.0

        outside_fence = value < lower or value > upper
        z_flag = robust_z >= self.robust_z_threshold
        anomaly_detected = bool(z_flag or outside_fence)

        # Normalise to ~[0, 1]: robust_z relative to its threshold, blended with
        # the IQR excess. inf maps to 1.0.
        if math.isinf(robust_z) or math.isinf(iqr_excess):
            score = 1.0
        else:
            z_component = robust_z / self.robust_z_threshold
            iqr_component = iqr_excess / 3.0  # ~3 IQR beyond fence -> saturate
            score = max(z_component, iqr_component)
            score = 1.0 - math.exp(-score)  # smooth squash into [0, 1)

        return {
            "anomaly_detected": anomaly_detected,
            "anomaly_score": round(max(0.0, min(1.0, score)), 4),
            "method": "robust_zscore_iqr",
        }
