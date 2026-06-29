"""Statistical anomaly detection for QC series (pure-Python).

A complement to the rule-based engines: rather than asking "did this breach a
threshold?", the anomaly detector asks "is this point unusual *relative to the
analyte's own recent behaviour*?". It fuses three cheap, robust signals:

* **z-score** vs the recent mean/SD (sensitive to large single excursions);
* **IQR** outlier test (robust to skew and a few extreme points);
* **rolling deviation** vs a short trailing window (sensitive to sudden shifts).

The combined ``anomaly_score`` is normalised to roughly ``[0, 1]`` so it can be
surfaced directly in :class:`~edge.qc_inference.schemas.AIInsights`.

Future upgrade
--------------
This is intentionally simple and dependency-free. A trained IsolationForest (or
the LSTM reconstruction error) can be dropped in behind the same interface when
a model has been synced from the cloud; until then these statistics give a
useful, explainable signal entirely offline.
"""

from __future__ import annotations

import math
from typing import Sequence

# z-score above which a point is considered anomalous.
Z_THRESHOLD = 3.0
# Multiplier on the IQR for the Tukey outlier fence.
IQR_FACTOR = 1.5
# Score >= this is reported as ``anomaly_detected = True``.
DETECTION_SCORE = 0.6


def _percentile(ordered: list[float], pct: float) -> float:
    """Linear-interpolation percentile over an already-sorted list."""
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[int(rank)]
    frac = rank - low
    return ordered[low] * (1.0 - frac) + ordered[high] * frac


class AnomalyDetector:
    """Detect statistically unusual QC points from a value plus its history."""

    def detect(self, value: float, history: Sequence[float] | None = None) -> dict:
        """Score how anomalous ``value`` is relative to ``history``.

        Args:
            value: current control measurement.
            history: previous control values (oldest first). Needs at least a
                few points to be meaningful; with too little data the detector
                abstains (``anomaly_detected = False``, low score).

        Returns:
            Dict with ``anomaly_detected`` (bool) and ``anomaly_score`` (float
            in ~[0, 1]), plus ``signals`` detailing each contributing test.
        """
        hist = [float(x) for x in (history or [])]
        if len(hist) < 4:
            # Not enough context to claim anything; abstain gracefully.
            return {
                "anomaly_detected": False,
                "anomaly_score": 0.0,
                "signals": {"reason": "insufficient_history", "n": len(hist)},
            }

        z_sig = self._z_signal(value, hist)
        iqr_sig = self._iqr_signal(value, hist)
        roll_sig = self._rolling_signal(value, hist)

        # Combine: the strongest single signal dominates, with the others
        # nudging the score up. Clamp to [0, 1].
        score = max(z_sig, iqr_sig, roll_sig)
        score = min(1.0, score + 0.1 * (z_sig + iqr_sig + roll_sig - score))
        score = max(0.0, min(1.0, score))

        return {
            "anomaly_detected": score >= DETECTION_SCORE,
            "anomaly_score": round(score, 4),
            "signals": {
                "z_score": round(z_sig, 4),
                "iqr": round(iqr_sig, 4),
                "rolling": round(roll_sig, 4),
            },
        }

    # ------------------------------------------------------------------ #
    # Individual signals (each returns a normalised [0, 1] contribution)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _z_signal(value: float, hist: list[float]) -> float:
        """Normalised |z-score| of ``value`` vs the history mean/SD."""
        n = len(hist)
        mu = sum(hist) / n
        ss = sum((x - mu) ** 2 for x in hist)
        sd = math.sqrt(ss / (n - 1)) if n > 1 else 0.0
        if sd == 0:
            return 1.0 if value != mu else 0.0
        z = abs(value - mu) / sd
        # Map |z| onto [0,1] saturating at Z_THRESHOLD.
        return min(1.0, z / Z_THRESHOLD)

    @staticmethod
    def _iqr_signal(value: float, hist: list[float]) -> float:
        """Tukey IQR outlier contribution: how far past the fence ``value`` is."""
        ordered = sorted(hist)
        q1 = _percentile(ordered, 25.0)
        q3 = _percentile(ordered, 75.0)
        iqr = q3 - q1
        if iqr == 0:
            return 0.0
        lower = q1 - IQR_FACTOR * iqr
        upper = q3 + IQR_FACTOR * iqr
        if lower <= value <= upper:
            return 0.0
        overshoot = (value - upper) if value > upper else (lower - value)
        # One full IQR past the fence -> full signal.
        return min(1.0, overshoot / iqr)

    @staticmethod
    def _rolling_signal(value: float, hist: list[float], window: int = 5) -> float:
        """Deviation of ``value`` from the trailing rolling mean, in SD units."""
        tail = hist[-window:]
        if len(tail) < 2:
            return 0.0
        mu = sum(tail) / len(tail)
        ss = sum((x - mu) ** 2 for x in tail)
        sd = math.sqrt(ss / (len(tail) - 1))
        if sd == 0:
            return 0.0
        dev = abs(value - mu) / sd
        return min(1.0, dev / Z_THRESHOLD)
