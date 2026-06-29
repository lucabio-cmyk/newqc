"""Lightweight QConnect non-Gaussian limits engine (pure-Python).

Many real-world QC distributions (especially serology signal-to-cutoff ratios)
are *not* Gaussian, so symmetric mean +/- k*SD limits over- or under-flag. The
QConnect engine instead uses *empirical percentile* limits derived from the
analyte's own history.

The control limits (LCL/UCL and the underlying percentiles) are computed in the
cloud from the full population and pushed down to the edge, where they are
**cached** (see :mod:`edge.qc_inference.cache`). This engine consumes those
cached limits and also positions the current value on the empirical CDF. When no
cached limits are available it degrades gracefully by deriving percentiles from
whatever local history it has.
"""

from __future__ import annotations

import math
from typing import Sequence

# Default lower/upper percentiles used to build the control band when the cached
# limits do not provide explicit LCL/UCL values.
LOWER_PERCENTILE = 5.0
UPPER_PERCENTILE = 95.0


def percentile(values: Sequence[float], pct: float) -> float:
    """Linear-interpolation percentile (matches numpy's default ``linear``).

    Args:
        values: numeric sequence (need not be sorted).
        pct: percentile in the inclusive range ``[0, 100]``.

    Returns:
        The interpolated percentile value, or ``0.0`` for an empty sequence.
    """
    if not values:
        return 0.0
    if not 0.0 <= pct <= 100.0:
        raise ValueError("pct must be in [0, 100]")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[int(rank)]
    frac = rank - low
    return ordered[low] * (1.0 - frac) + ordered[high] * frac


def empirical_cdf_position(value: float, history: Sequence[float]) -> float:
    """Fraction of ``history`` observations <= ``value`` (empirical CDF in [0,1])."""
    if not history:
        return 0.5
    return sum(1 for x in history if x <= value) / len(history)


class QConnectEngine:
    """Evaluate a QC point against cached empirical percentile limits."""

    def evaluate(self, value: float, limits: dict | None = None) -> dict:
        """Evaluate ``value`` against percentile control limits.

        Args:
            value: current control measurement.
            limits: cached limits dict pushed from the cloud. Recognised keys:
                ``lcl``, ``ucl`` (explicit control limits),
                ``percentile_5``, ``percentile_95`` (band endpoints),
                ``history`` (list[float] used for the empirical CDF position).
                Any subset may be present; missing pieces are derived from
                ``history`` when available.

        Returns:
            Dict matching the ``QConnectResult`` contract fragment:
            ``status``, ``percentile_5``, ``percentile_95``,
            ``percentile_position``, ``lcl``, ``ucl``.
        """
        limits = dict(limits or {})
        history: list[float] = list(limits.get("history") or [])

        # Resolve the band endpoints, preferring explicit cached values and
        # falling back to percentiles over the local history.
        p5 = self._coalesce(
            limits.get("percentile_5"),
            limits.get("lcl"),
            (percentile(history, LOWER_PERCENTILE) if history else None),
        )
        p95 = self._coalesce(
            limits.get("percentile_95"),
            limits.get("ucl"),
            (percentile(history, UPPER_PERCENTILE) if history else None),
        )

        lcl = self._coalesce(limits.get("lcl"), p5)
        ucl = self._coalesce(limits.get("ucl"), p95)

        # If we still have nothing to compare against, we cannot judge; report a
        # neutral REVIEW_REQUIRED so the result is never silently passed.
        if lcl is None or ucl is None:
            return {
                "status": "REVIEW_REQUIRED",
                "percentile_5": float(p5 or 0.0),
                "percentile_95": float(p95 or 0.0),
                "percentile_position": 0.5,
                "lcl": float(lcl or 0.0),
                "ucl": float(ucl or 0.0),
            }

        position = empirical_cdf_position(value, history) if history else 0.5
        status = "PASS" if lcl <= value <= ucl else "FAIL"

        return {
            "status": status,
            "percentile_5": round(float(p5), 6),
            "percentile_95": round(float(p95), 6),
            "percentile_position": round(float(position), 6),
            "lcl": round(float(lcl), 6),
            "ucl": round(float(ucl), 6),
        }

    def compare_with_westgard(self, qconnect_result: dict, westgard_result: dict) -> dict:
        """Reconcile the QConnect and Westgard verdicts.

        QConnect tends to be more tolerant of legitimately skewed distributions,
        while Westgard is more sensitive to systematic drift. When the engines
        disagree we surface a REVIEW_REQUIRED so a human adjudicates.

        Args:
            qconnect_result: output of :meth:`evaluate`.
            westgard_result: output of ``WestgardEngine.evaluate``.

        Returns:
            Dict with ``agreement`` (bool), ``combined_status`` and ``note``.
        """
        q = qconnect_result.get("status")
        w = westgard_result.get("status")
        if q == w:
            return {
                "agreement": True,
                "combined_status": q,
                "note": "Westgard and QConnect agree.",
            }
        # Any FAIL is taken seriously; mixed signals -> human review.
        if "FAIL" in (q, w):
            combined = "REVIEW_REQUIRED"
            note = "Engines disagree (one FAIL); flag for review."
        else:
            combined = "REVIEW_REQUIRED"
            note = "Engines disagree; flag for review."
        return {"agreement": False, "combined_status": combined, "note": note}

    @staticmethod
    def _coalesce(*candidates: float | None) -> float | None:
        """Return the first non-``None`` candidate, else ``None``."""
        for c in candidates:
            if c is not None:
                return float(c)
        return None
