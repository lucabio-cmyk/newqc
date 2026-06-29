"""QConnect non-Gaussian QC engine (numpy/scipy-optional).

Why a second engine?
--------------------
The classic Westgard rules assume QC values are **Gaussian** around a fixed
mean. Many serology / infectious-disease assays (HIV, HCV, HBsAg, syphilis)
report signal-to-cutoff (S/CO) ratios whose negative-control distributions are
strongly **right-skewed** or even **bimodal** (two reagent populations, lot
transitions). On such data, ``mean +/- k*SD`` limits are wrong: the lower
``-3SD`` limit can fall below zero and the upper limit rejects perfectly normal
high-tail values. The result is a false-rejection storm.

QConnect instead builds **empirical / percentile-based** limits directly from
the observed distribution (and optionally a kernel-density estimate), so the
control limits track the real shape of the data. It is the recommended approach
whenever :class:`models.distribution.DistributionDetector` flags a non-Gaussian
or bimodal distribution.

EDCNet peer-data hook
---------------------
``calculate_kde_limits`` and ``calculate_percentiles`` currently use only the
lab's local history. TODO(EDCNet): blend in anonymised peer-group data from the
External Data Comparison Network so a freshly deployed lot with little local
history inherits robust limits from the federated peer distribution.
"""

from __future__ import annotations

import math
from typing import Sequence

from loguru import logger

try:  # numpy is optional.
    import numpy as _np  # type: ignore

    _HAS_NUMPY = True
except Exception:  # pragma: no cover
    _np = None  # type: ignore
    _HAS_NUMPY = False

try:  # scipy is optional — KDE falls back to percentiles without it.
    from scipy import stats as _scipy_stats  # type: ignore

    _HAS_SCIPY = True
except Exception:  # pragma: no cover
    _scipy_stats = None  # type: ignore
    _HAS_SCIPY = False


def _percentile(values: Sequence[float], pct: float) -> float:
    """Linear-interpolation percentile (matches numpy's default method).

    Pure-Python so it works without numpy.
    """
    if not values:
        return 0.0
    if not 0 <= pct <= 100:
        raise ValueError("pct must be in [0, 100]")
    ordered = sorted(float(x) for x in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[int(rank)]
    frac = rank - low
    return ordered[low] * (1 - frac) + ordered[high] * frac


class QConnectEngine:
    """Empirical / percentile-based QC limits for non-Gaussian analytes."""

    LOWER_PCT = 5.0
    UPPER_PCT = 95.0

    def __init__(self) -> None:
        self._log = logger.bind(engine="qconnect")

    # ------------------------------------------------------------------ #
    # Percentiles
    # ------------------------------------------------------------------ #
    def calculate_percentiles(self, history: Sequence[float]) -> dict[str, float]:
        """Return ``{p5, p25, p50, p75, p95}`` for the history.

        TODO(EDCNet): when local ``history`` is short, augment with peer data.
        """
        return {
            "p5": _percentile(history, 5.0),
            "p25": _percentile(history, 25.0),
            "p50": _percentile(history, 50.0),
            "p75": _percentile(history, 75.0),
            "p95": _percentile(history, 95.0),
        }

    # ------------------------------------------------------------------ #
    # Bimodality
    # ------------------------------------------------------------------ #
    def detect_bimodality(self, history: Sequence[float]) -> dict:
        """Estimate whether the distribution is bimodal.

        Uses a simple bimodality coefficient::

            BC = (skew^2 + 1) / (kurtosis_excess + 3)

        BC > 0.555 (the uniform-distribution benchmark) is suggestive of
        bimodality. Returns ``{is_bimodal, score}`` where ``score`` is BC.
        """
        values = [float(x) for x in history]
        n = len(values)
        if n < 4:
            return {"is_bimodal": False, "score": 0.0}

        mean = sum(values) / n
        m2 = sum((x - mean) ** 2 for x in values) / n
        if m2 == 0:
            return {"is_bimodal": False, "score": 0.0}
        m3 = sum((x - mean) ** 3 for x in values) / n
        m4 = sum((x - mean) ** 4 for x in values) / n
        skew = m3 / (m2**1.5)
        kurt_excess = m4 / (m2**2) - 3.0
        # Sarle's bimodality coefficient.
        bc = (skew**2 + 1.0) / (kurt_excess + 3.0)
        is_bimodal = bc > 0.555
        self._log.debug("bimodality", bc=round(bc, 4), is_bimodal=is_bimodal)
        return {"is_bimodal": is_bimodal, "score": bc}

    # ------------------------------------------------------------------ #
    # KDE-based limits
    # ------------------------------------------------------------------ #
    def calculate_kde_limits(self, history: Sequence[float]) -> dict[str, float]:
        """Compute lower/upper control limits.

        When scipy is available a Gaussian KDE is fit and the limits are taken
        as the 5th/95th percentiles of a fine resample of the estimated density
        support; otherwise we fall back to empirical percentiles. Either way the
        limits respect the real (possibly skewed) shape of the data.

        TODO(EDCNet): mix the local KDE with the peer-group KDE.
        """
        values = [float(x) for x in history]
        if len(values) < 5:
            # Too little data for a stable estimate — use raw percentiles.
            return {
                "lcl": _percentile(values, self.LOWER_PCT),
                "ucl": _percentile(values, self.UPPER_PCT),
            }

        if _HAS_SCIPY and _HAS_NUMPY:
            try:
                arr = _np.asarray(values, dtype=float)
                kde = _scipy_stats.gaussian_kde(arr)
                lo, hi = float(arr.min()), float(arr.max())
                span = hi - lo or 1.0
                grid = _np.linspace(lo - 0.1 * span, hi + 0.1 * span, 1024)
                density = kde(grid)
                cdf = _np.cumsum(density)
                cdf = cdf / cdf[-1]
                lcl = float(_np.interp(self.LOWER_PCT / 100.0, cdf, grid))
                ucl = float(_np.interp(self.UPPER_PCT / 100.0, cdf, grid))
                return {"lcl": lcl, "ucl": ucl}
            except Exception as exc:  # pragma: no cover - numerical guard
                self._log.warning("KDE failed, falling back to percentiles: {}", exc)

        return {
            "lcl": _percentile(values, self.LOWER_PCT),
            "ucl": _percentile(values, self.UPPER_PCT),
        }

    # ------------------------------------------------------------------ #
    # Evaluate
    # ------------------------------------------------------------------ #
    def evaluate(self, value: float, limits: dict) -> dict:
        """Evaluate a value against QConnect limits.

        Args:
            value: the new QC measurement.
            limits: a dict that may contain pre-computed ``lcl``/``ucl`` and a
                ``history`` list. Missing limits are derived from ``history``.

        Returns:
            A dict matching :class:`schemas.QConnectResult`::

                {status, percentile_5, percentile_95, percentile_position,
                 lcl, ucl}
        """
        history = [float(x) for x in limits.get("history", [])]
        # Whether we have *any* basis to judge this value: a peer/lot history or
        # explicitly assigned limits. With neither (cold start) the engine must
        # abstain rather than flag every first-ever result for review.
        has_explicit_limits = ("lcl" in limits and "ucl" in limits) or (
            "percentile_5" in limits and "percentile_95" in limits
        )
        has_basis = bool(history) or has_explicit_limits

        pctiles = self.calculate_percentiles(history) if history else {}
        p5 = limits.get("percentile_5", pctiles.get("p5", value))
        p95 = limits.get("percentile_95", pctiles.get("p95", value))

        if "lcl" in limits and "ucl" in limits:
            lcl, ucl = float(limits["lcl"]), float(limits["ucl"])
        elif history:
            kde = self.calculate_kde_limits(history)
            lcl, ucl = kde["lcl"], kde["ucl"]
        else:
            lcl, ucl = p5, p95

        # Empirical CDF position of the value within the history.
        if history:
            position = sum(1 for x in history if x <= value) / len(history)
        else:
            position = 0.5

        if not has_basis:
            # No history and no assigned limits: nothing to compare against.
            # Abstain (PASS); Westgard + the assigned target/SD govern this run.
            status = "PASS"
        elif value < lcl or value > ucl:
            status = "FAIL"
        elif value <= p5 or value >= p95:
            status = "REVIEW_REQUIRED"
        else:
            status = "PASS"

        self._log.debug(
            "qconnect evaluated",
            value=value,
            lcl=round(lcl, 4),
            ucl=round(ucl, 4),
            status=status,
        )

        return {
            "status": status,
            "percentile_5": float(p5),
            "percentile_95": float(p95),
            "percentile_position": float(position),
            "lcl": float(lcl),
            "ucl": float(ucl),
        }

    # ------------------------------------------------------------------ #
    # Cross-check vs Westgard
    # ------------------------------------------------------------------ #
    def compare_with_westgard(self, qc_data: dict, westgard_result: dict) -> dict:
        """Flag discordance between QConnect and Westgard verdicts.

        Discordance (one engine passes while the other rejects) is the single
        strongest signal that the Gaussian assumption is wrong for this analyte.
        When detected we recommend switching the analyte to the QConnect
        approach and routing the result to human review.

        Args:
            qc_data: dict with at least the QConnect ``status``.
            westgard_result: the Westgard result dict.
        """
        q_status = qc_data.get("status")
        w_status = westgard_result.get("status")
        passes = {"PASS"}
        q_pass = q_status in passes
        w_pass = w_status in passes
        discordance = q_pass != w_pass

        if discordance:
            recommendation = (
                "Westgard and QConnect disagree — the Gaussian assumption is "
                "likely invalid for this analyte. Route to manual review and "
                "consider migrating this analyte to the QConnect approach."
            )
        else:
            recommendation = "Engines concordant; no action."

        self._log.debug(
            "westgard/qconnect comparison",
            qconnect=q_status,
            westgard=w_status,
            discordance=discordance,
        )
        return {
            "discordance_detected": discordance,
            "recommendation": recommendation,
        }
