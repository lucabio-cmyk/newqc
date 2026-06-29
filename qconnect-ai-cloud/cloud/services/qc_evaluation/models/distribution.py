"""Distribution detector (scipy-optional with pure-Python fallbacks).

Picking the right QC engine for an analyte hinges on the *shape* of its control
distribution:

* **Gaussian** -> classic Westgard ``mean +/- k*SD`` limits are valid.
* **Non-Gaussian / skewed / bimodal** -> percentile-based QConnect limits.

This module characterises a history of control values (normality test, skewness,
kurtosis, bimodality) and returns a recommended QC approach. scipy is used when
available (``shapiro``, ``skew``, ``kurtosis``); otherwise pure-Python moment
estimators are used so the detector still works in minimal environments.
"""

from __future__ import annotations

from typing import Sequence

from loguru import logger

try:
    from scipy import stats as _scipy_stats  # type: ignore

    _HAS_SCIPY = True
except Exception:  # pragma: no cover
    _scipy_stats = None  # type: ignore
    _HAS_SCIPY = False

# Distribution labels (kept consistent with shared.constants).
DISTRIBUTION_GAUSSIAN = "gaussian"
DISTRIBUTION_SKEWED = "skewed"
DISTRIBUTION_BIMODAL = "bimodal"
DISTRIBUTION_UNKNOWN = "unknown"

SHAPIRO_WILK_ALPHA = 0.05
MIN_SAMPLES_FOR_DISTRIBUTION = 30


class DistributionDetector:
    """Classify the distribution of a control-value history."""

    def __init__(self) -> None:
        self._log = logger.bind(engine="distribution")

    # ------------------------------------------------------------------ #
    # Moments (pure-Python fallbacks)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _moments(values: Sequence[float]) -> tuple[float, float, float, float]:
        """Return ``(mean, variance, skewness, excess_kurtosis)``.

        Population moments; pure-Python so no numpy/scipy required.
        """
        n = len(values)
        if n == 0:
            return 0.0, 0.0, 0.0, 0.0
        mean = sum(values) / n
        m2 = sum((x - mean) ** 2 for x in values) / n
        if m2 == 0:
            return mean, 0.0, 0.0, 0.0
        m3 = sum((x - mean) ** 3 for x in values) / n
        m4 = sum((x - mean) ** 4 for x in values) / n
        skew = m3 / (m2**1.5)
        excess_kurt = m4 / (m2**2) - 3.0
        return mean, m2, skew, excess_kurt

    def _shapiro_p(self, values: Sequence[float]) -> float | None:
        """Shapiro-Wilk p-value when scipy is present, else ``None``.

        scipy requires 3 <= n <= 5000 for ``shapiro``.
        """
        n = len(values)
        if not _HAS_SCIPY or n < 3 or n > 5000:
            return None
        try:
            _, p = _scipy_stats.shapiro(list(values))
            return float(p)
        except Exception as exc:  # pragma: no cover - numerical guard
            self._log.warning("shapiro failed: {}", exc)
            return None

    @staticmethod
    def _bimodality_coefficient(skew: float, excess_kurt: float) -> float:
        """Sarle's bimodality coefficient ``(skew^2 + 1)/(kurt_excess + 3)``."""
        denom = excess_kurt + 3.0
        if denom == 0:
            return 0.0
        return (skew**2 + 1.0) / denom

    # ------------------------------------------------------------------ #
    # Detection
    # ------------------------------------------------------------------ #
    def detect(self, history: Sequence[float]) -> dict:
        """Classify the distribution and recommend a QC approach.

        Returns a dict::

            {detected_distribution, confidence, shapiro_wilk_p, skewness,
             kurtosis, bimodal_score, recommended_qc_approach, sample_size}

        Decision logic:
            * Bimodality coefficient > 0.555 -> ``bimodal`` -> QConnect.
            * |skewness| > 1.0 -> ``skewed`` -> QConnect.
            * Shapiro p >= alpha (or, without scipy, near-symmetric & mesokurtic)
              -> ``gaussian`` -> Westgard.
            * Otherwise non-gaussian -> QConnect.
        """
        values = [float(x) for x in history]
        n = len(values)

        if n < 3:
            return {
                "detected_distribution": DISTRIBUTION_UNKNOWN,
                "confidence": 0.0,
                "shapiro_wilk_p": None,
                "skewness": 0.0,
                "kurtosis": 0.0,
                "bimodal_score": 0.0,
                "recommended_qc_approach": "westgard",  # conservative default
                "sample_size": n,
            }

        if _HAS_SCIPY and n >= 8:
            try:
                skew = float(_scipy_stats.skew(values))
                kurt = float(_scipy_stats.kurtosis(values))  # excess kurtosis
            except Exception:  # pragma: no cover
                _, _, skew, kurt = self._moments(values)
        else:
            _, _, skew, kurt = self._moments(values)

        shapiro_p = self._shapiro_p(values)
        bimodal_score = self._bimodality_coefficient(skew, kurt)

        # --- classify -------------------------------------------------- #
        confidence = 0.5
        if bimodal_score > 0.555 and n >= 8:
            distribution = DISTRIBUTION_BIMODAL
            approach = "qconnect"
            confidence = min(0.95, 0.6 + (bimodal_score - 0.555))
        elif abs(skew) > 1.0:
            distribution = DISTRIBUTION_SKEWED
            approach = "qconnect"
            confidence = min(0.95, 0.6 + (abs(skew) - 1.0) * 0.2)
        elif shapiro_p is not None:
            if shapiro_p >= SHAPIRO_WILK_ALPHA:
                distribution = DISTRIBUTION_GAUSSIAN
                approach = "westgard"
                confidence = min(0.95, 0.6 + shapiro_p)
            else:
                distribution = DISTRIBUTION_SKEWED
                approach = "qconnect"
                confidence = min(0.95, 0.6 + (SHAPIRO_WILK_ALPHA - shapiro_p))
        else:
            # No scipy: judge by moments. Near-symmetric & mesokurtic -> gaussian.
            if abs(skew) < 0.5 and abs(kurt) < 1.0:
                distribution = DISTRIBUTION_GAUSSIAN
                approach = "westgard"
                confidence = 0.65
            else:
                distribution = DISTRIBUTION_SKEWED
                approach = "qconnect"
                confidence = 0.6

        # Low confidence whenever the sample is too small to trust.
        if n < MIN_SAMPLES_FOR_DISTRIBUTION:
            confidence = min(confidence, 0.6)

        self._log.debug(
            "distribution detected",
            distribution=distribution,
            skew=round(skew, 3),
            kurt=round(kurt, 3),
            bimodal=round(bimodal_score, 3),
            shapiro_p=shapiro_p,
            n=n,
        )

        return {
            "detected_distribution": distribution,
            "confidence": round(confidence, 4),
            "shapiro_wilk_p": shapiro_p,
            "skewness": round(skew, 6),
            "kurtosis": round(kurt, 6),
            "bimodal_score": round(bimodal_score, 6),
            "recommended_qc_approach": approach,
            "sample_size": n,
        }
