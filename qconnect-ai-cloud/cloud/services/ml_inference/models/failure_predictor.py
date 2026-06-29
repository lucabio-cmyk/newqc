"""48-hour QC failure-probability predictor (transparent heuristic).

The :class:`FailurePredictor` combines four interpretable features extracted
from the current QC point and its recent history into a single logistic-squashed
probability in ``[0, 1]``:

* **z-score magnitude** — how far the current value sits from target in SD units.
* **trend / slope** — least-squares slope of the recent history (per point),
  normalised by SD; a sustained drift toward the limits raises risk.
* **drift (mean shift)** — difference between the recent-history mean and the
  target, in SD units (a shifted process is more likely to breach soon).
* **increasing variance** — variance of the second half of history relative to
  the first half; widening dispersion signals instability.

This is NOT a trained model. The weights are fixed, hand-tuned constants chosen
so that bigger deviations / trends / drift / variance monotonically increase the
predicted probability. Everything is deterministic and explainable.

numpy is used when available for the linear fit and moments, but a pure-python
fallback keeps the module importable and correct without it.
"""

from __future__ import annotations

import math

try:  # numpy is optional; pure-python fallbacks below cover the same maths.
    import numpy as _np
except Exception:  # pragma: no cover - numpy is installed in this environment.
    _np = None


# Fixed, hand-tuned feature weights (logit space). Larger -> more influence.
_WEIGHTS: dict[str, float] = {
    "zscore": 1.10,
    "trend": 0.90,
    "drift": 0.80,
    "variance": 0.55,
}
_BIAS = -2.6  # baseline logit so a perfectly stable, on-target point -> low risk

# Per-point timeline used to translate "48h" into a horizon for reporting.
_TIMELINE_HOURS = 48.0


def _slope(values: list[float]) -> float:
    """Least-squares slope of ``values`` against their index (per point).

    Returns ``0.0`` for fewer than two points.
    """
    n = len(values)
    if n < 2:
        return 0.0
    if _np is not None:
        x = _np.arange(n, dtype=float)
        y = _np.asarray(values, dtype=float)
        # polyfit deg=1 -> [slope, intercept]
        slope = float(_np.polyfit(x, y, 1)[0])
        return slope
    # Pure-python OLS slope.
    mean_x = (n - 1) / 2.0
    mean_y = sum(values) / n
    num = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(values))
    den = sum((i - mean_x) ** 2 for i in range(n))
    return num / den if den else 0.0


def _mean(values: list[float]) -> float:
    """Arithmetic mean (``0.0`` for an empty sequence)."""
    if not values:
        return 0.0
    if _np is not None:
        return float(_np.mean(values))
    return sum(values) / len(values)


def _variance(values: list[float]) -> float:
    """Population variance (``0.0`` for fewer than two points)."""
    n = len(values)
    if n < 2:
        return 0.0
    if _np is not None:
        return float(_np.var(values))
    m = sum(values) / n
    return sum((v - m) ** 2 for v in values) / n


def _logistic(x: float) -> float:
    """Numerically stable logistic squash into ``(0, 1)``."""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _risk_level(probability: float) -> str:
    """Map a probability to a categorical risk level."""
    if probability >= 0.8:
        return "critical"
    if probability >= 0.6:
        return "high"
    if probability >= 0.3:
        return "medium"
    return "low"


class FailurePredictor:
    """Heuristic predictor of 48h QC failure probability.

    The model is stateless; all parameters are the fixed module-level weights.
    """

    weights = _WEIGHTS
    bias = _BIAS
    timeline_hours = _TIMELINE_HOURS

    def features(
        self,
        result_value: float,
        target_value: float,
        sd_value: float,
        history: list[float],
    ) -> dict[str, float]:
        """Extract the four interpretable features (each scaled in SD units).

        Returns a dict ``{zscore, trend, drift, variance}``. Values are signed
        magnitudes already normalised so the weights operate on comparable
        scales. The history is taken oldest -> newest.
        """
        sd = float(sd_value) or 1.0
        hist = [float(x) for x in history]

        # Current deviation from target, in SD units.
        zscore = abs(float(result_value) - float(target_value)) / sd

        # Recent trend: slope per point normalised by SD, magnitude only.
        slope = _slope(hist)
        trend = abs(slope) / sd

        # Drift: how far the recent mean has shifted from target, in SD units.
        drift = abs(_mean(hist) - float(target_value)) / sd if hist else 0.0

        # Increasing variance: dispersion of the later half vs. the earlier half.
        variance_signal = 0.0
        if len(hist) >= 4:
            mid = len(hist) // 2
            early_var = _variance(hist[:mid])
            late_var = _variance(hist[mid:])
            # Ratio above 1 means widening spread; clamp the baseline.
            ratio = (late_var + 1e-9) / (early_var + 1e-9)
            variance_signal = max(0.0, math.log(ratio)) if ratio > 0 else 0.0

        return {
            "zscore": round(zscore, 6),
            "trend": round(trend, 6),
            "drift": round(drift, 6),
            "variance": round(variance_signal, 6),
        }

    def predict(
        self,
        result_value: float,
        target_value: float,
        sd_value: float,
        history: list[float],
    ) -> dict:
        """Predict 48h failure probability and explain the contributing factors.

        Returns ``{failure_probability_48h, timeline_hours, failure_risk_level,
        contributing_factors}`` where ``contributing_factors`` is a list of
        ``{name, value, weight, contribution}`` dicts ranked by absolute
        contribution to the logit.
        """
        feats = self.features(result_value, target_value, sd_value, history or [])

        contributions: list[dict] = []
        logit = self.bias
        for name, value in feats.items():
            weight = self.weights[name]
            contribution = weight * value
            logit += contribution
            contributions.append(
                {
                    "name": name,
                    "value": round(value, 4),
                    "weight": weight,
                    "contribution": round(contribution, 4),
                }
            )

        probability = _logistic(logit)
        probability = max(0.0, min(1.0, probability))

        contributions.sort(key=lambda c: abs(c["contribution"]), reverse=True)

        return {
            "failure_probability_48h": round(probability, 4),
            "timeline_hours": self.timeline_hours,
            "failure_risk_level": _risk_level(probability),
            "contributing_factors": contributions,
        }
