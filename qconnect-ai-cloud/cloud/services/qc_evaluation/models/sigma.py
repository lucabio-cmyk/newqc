"""Six Sigma quality engine for analytical methods (pure-Python).

The sigma metric expresses how many standard deviations of analytical
imprecision fit inside the allowable error budget::

    sigma = (TEa - |bias|) / CV

where all terms are in **percent** of the target:

* ``TEa``  — total allowable error (regulatory / biological goal, e.g. CLIA).
* ``bias`` — systematic inaccuracy of the method vs the reference.
* ``CV``   — analytical imprecision (coefficient of variation).

Interpretation (Westgard sigma categories):

======  ==================  =======================================
sigma   category            meaning
======  ==================  =======================================
>= 6    ">6"                world-class; a single 1-3S rule suffices
4 - 6   "4-6"               good; modest multirule set
3 - 4   "3-4"               marginal; full multirule + more controls
2 - 3   "2-3"               poor; maximal multirule, high QC effort
< 2     "<2"                method not fit for purpose
======  ==================  =======================================

Diagnostic sigma
----------------
Classic sigma ignores the *clinical* consequence of an error. For qualitative
serology a false negative (missed HIV) is catastrophic while a false positive is
merely costly. :meth:`SigmaEngine.diagnostic_sigma` computes sensitivity /
specificity / PPV / NPV from a confusion matrix and folds in caller-supplied
consequence weights to produce a consequence-weighted diagnostic sigma.
"""

from __future__ import annotations

from loguru import logger

# (lower_bound_inclusive, label, recommended_rules) — most stringent first.
SIGMA_CATEGORIES: tuple[tuple[float, str, str], ...] = (
    (6.0, ">6", "1-3S (N=2)"),
    (4.0, "4-6", "1-3S / 2-2S / R-4S (N=2)"),
    (3.0, "3-4", "1-3S / 2-2S / R-4S / 4-1S (N=4)"),
    (2.0, "2-3", "1-3S / 2-2S / R-4S / 4-1S / 8x (N=4, multirule)"),
    (0.0, "<2", "Method not fit for purpose - investigate"),
)

# Coarse expected false-rejection-rate (%) per category for an N=2 design.
# These are illustrative planning figures, not exact OPSpecs values.
_EXPECTED_FRR_PERCENT: dict[str, float] = {
    ">6": 0.0,
    "4-6": 1.0,
    "3-4": 3.0,
    "2-3": 5.0,
    "<2": 10.0,
}


class SigmaEngine:
    """Compute analytical and diagnostic Six Sigma metrics."""

    def __init__(self) -> None:
        self._log = logger.bind(engine="sigma")

    # ------------------------------------------------------------------ #
    # Analytical sigma
    # ------------------------------------------------------------------ #
    def calculate_sigma(
        self,
        bias_percent: float,
        cv_percent: float,
        allowable_error_percent: float,
    ) -> float:
        """Return the analytical sigma metric ``(TEa - |bias|) / CV``.

        Guards against a zero/near-zero CV (returns a large finite sentinel) and
        never returns a negative sigma (clamped at 0.0).
        """
        if cv_percent <= 0:
            # Perfect precision -> effectively unbounded sigma; cap for sanity.
            self._log.debug("cv<=0, returning capped sigma")
            return 99.9
        sigma = (allowable_error_percent - abs(bias_percent)) / cv_percent
        return max(0.0, sigma)

    # ------------------------------------------------------------------ #
    # Categorisation
    # ------------------------------------------------------------------ #
    def categorize(self, sigma: float) -> tuple[str, str]:
        """Map a sigma metric to ``(label, recommended_rules)``."""
        for lower, label, rules in SIGMA_CATEGORIES:
            if sigma >= lower:
                return label, rules
        return "<2", "Method not fit for purpose - investigate"

    # ------------------------------------------------------------------ #
    # Full evaluation
    # ------------------------------------------------------------------ #
    def evaluate(
        self,
        bias_percent: float,
        cv_percent: float,
        allowable_error_percent: float,
    ) -> dict:
        """Compute a :class:`schemas.SigmaResult`-shaped dict.

        Returns ``{sigma_metric, sigma_category, recommended_rules,
        expected_frr_percent}``.
        """
        sigma = self.calculate_sigma(bias_percent, cv_percent, allowable_error_percent)
        label, rules = self.categorize(sigma)
        frr = _EXPECTED_FRR_PERCENT.get(label, 5.0)
        self._log.debug(
            "sigma evaluated",
            sigma=round(sigma, 2),
            category=label,
        )
        return {
            "sigma_metric": round(sigma, 4),
            "sigma_category": label,
            "recommended_rules": rules,
            "expected_frr_percent": frr,
        }

    # ------------------------------------------------------------------ #
    # Diagnostic (consequence-weighted) sigma
    # ------------------------------------------------------------------ #
    def diagnostic_sigma(
        self,
        tp: int,
        tn: int,
        fp: int,
        fn: int,
        weight_fn: float = 1.0,
        weight_fp: float = 1.0,
    ) -> dict:
        """Compute diagnostic performance metrics and a weighted sigma.

        Args:
            tp, tn, fp, fn: confusion-matrix counts.
            weight_fn: clinical consequence weight of a false negative
                (e.g. a missed HIV-positive). Higher -> errors hurt more.
            weight_fp: clinical consequence weight of a false positive.

        Returns:
            ``{sensitivity, specificity, ppv, npv, defects_per_million,
            diagnostic_sigma, weighted_defect_rate}``.

        The diagnostic sigma is derived from the consequence-weighted defect
        rate. We map a weighted defect proportion ``p`` to a short-term sigma
        via the standard DPMO ladder approximation::

            sigma ~ 0.8406 + sqrt(29.37 - 2.221 * ln(DPMO))

        clamped to ``[0, 6]``.
        """
        total = tp + tn + fp + fn
        if total == 0:
            return {
                "sensitivity": 0.0,
                "specificity": 0.0,
                "ppv": 0.0,
                "npv": 0.0,
                "defects_per_million": 0.0,
                "diagnostic_sigma": 0.0,
                "weighted_defect_rate": 0.0,
            }

        sensitivity = tp / (tp + fn) if (tp + fn) else 0.0
        specificity = tn / (tn + fp) if (tn + fp) else 0.0
        ppv = tp / (tp + fp) if (tp + fp) else 0.0
        npv = tn / (tn + fn) if (tn + fn) else 0.0

        # Consequence-weighted defect rate: weight each error type by its
        # clinical cost, normalised by the weighted total opportunities.
        weighted_defects = weight_fn * fn + weight_fp * fp
        weighted_total = weighted_defects + tp + tn
        weighted_defect_rate = weighted_defects / weighted_total if weighted_total else 0.0

        dpmo = weighted_defect_rate * 1_000_000.0
        diagnostic_sigma = self._sigma_from_dpmo(dpmo)

        self._log.debug(
            "diagnostic sigma",
            sensitivity=round(sensitivity, 4),
            specificity=round(specificity, 4),
            dpmo=round(dpmo, 1),
            diagnostic_sigma=round(diagnostic_sigma, 3),
        )

        return {
            "sensitivity": round(sensitivity, 6),
            "specificity": round(specificity, 6),
            "ppv": round(ppv, 6),
            "npv": round(npv, 6),
            "defects_per_million": round(dpmo, 2),
            "diagnostic_sigma": round(diagnostic_sigma, 4),
            "weighted_defect_rate": round(weighted_defect_rate, 6),
        }

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _sigma_from_dpmo(dpmo: float) -> float:
        """Convert defects-per-million-opportunities to a short-term sigma.

        Uses the common Schmidt/Launsby approximation, clamped to ``[0, 6]``.
        Zero defects map to the ceiling (6).
        """
        import math

        if dpmo <= 0:
            return 6.0
        if dpmo >= 1_000_000:
            return 0.0
        try:
            sigma = 0.8406 + math.sqrt(max(0.0, 29.37 - 2.221 * math.log(dpmo)))
        except ValueError:  # pragma: no cover - guarded above
            sigma = 0.0
        return max(0.0, min(6.0, sigma))
