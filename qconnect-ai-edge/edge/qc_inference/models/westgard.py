"""Lightweight Westgard multirule QC engine (pure-Python).

The Westgard rules are the de-facto standard for statistical QC in clinical
laboratories. They flag *random* error (1-3S, R-4S) and *systematic* error
(2-2S, 4-1S, 10x, 7T) by inspecting the most recent control measurements
expressed in standard-deviation (SD) units around the assigned mean.

This implementation deliberately avoids numpy/scipy so it can run inside the
edge inference container within the <100ms latency budget. ``history`` is the
chronologically ordered list of *previous* control values (oldest first); the
current ``value`` is appended internally for rules that need the running series.
"""

from __future__ import annotations

import math
from typing import Sequence

# Canonical rule identifiers (most specific first). Kept in sync with the shared
# package's ``WESTGARD_RULES`` constant.
RULES_CHECKED: tuple[str, ...] = ("1-2S", "1-3S", "2-2S", "R-4S", "4-1S", "10x", "7T")


class WestgardEngine:
    """Evaluate a single QC point against the classic Westgard multirules.

    The engine is stateless; all required history is passed to :meth:`evaluate`.
    """

    #: Warning threshold in SD units (1-2S is a warning that gates rejection).
    WARN_SD: float = 2.0
    #: Rejection threshold for a single point (1-3S).
    REJECT_SD: float = 3.0

    def calculate_stats(self, values: Sequence[float]) -> dict[str, float]:
        """Return descriptive statistics for a series.

        Args:
            values: numeric control values.

        Returns:
            Dict with ``mean``, ``sd`` (sample SD, n-1) and ``cv_percent``.
        """
        n = len(values)
        if n == 0:
            return {"mean": 0.0, "sd": 0.0, "cv_percent": 0.0}
        mu = sum(values) / n
        if n < 2:
            return {"mean": mu, "sd": 0.0, "cv_percent": 0.0}
        ss = sum((x - mu) ** 2 for x in values)
        sd = math.sqrt(ss / (n - 1))
        cv = (sd / mu * 100.0) if mu else 0.0
        return {"mean": mu, "sd": sd, "cv_percent": cv}

    def evaluate(
        self,
        value: float,
        target: float,
        sd: float,
        history: Sequence[float] | None = None,
    ) -> dict:
        """Evaluate ``value`` against the Westgard multirules.

        Args:
            value: current control measurement.
            target: assigned mean for the QC lot.
            sd: assigned SD for the QC lot (must be > 0).
            history: previous control values, oldest first (current value excluded).

        Returns:
            Dict matching the ``WestgardResult`` contract fragment:
            ``status`` (PASS/FAIL/REVIEW_REQUIRED), ``rule_violated``,
            ``rules_checked``, ``mean``, ``sd``, ``cv_percent``, ``deviation_sd``.
        """
        history = list(history or [])
        # Full chronological series with the current point appended last.
        series = history + [value]
        # SD-distance of every point from the assigned target/mean.
        zs = [self._z(x, target, sd) for x in series]
        z_now = zs[-1]

        cv_percent = (sd / target * 100.0) if target else 0.0
        rule_violated: str | None = None
        status = "PASS"

        # --- 1-3S: one point beyond +/-3 SD -> random error, reject. ------- #
        if abs(z_now) > self.REJECT_SD:
            rule_violated, status = "1-3S", "FAIL"

        # --- R-4S: range between the two most recent points >= 4 SD. ------- #
        # Detects random error even when neither point breaches 1-3S alone.
        elif len(zs) >= 2 and abs(zs[-1] - zs[-2]) >= 4.0:
            rule_violated, status = "R-4S", "FAIL"

        # --- 2-2S: two consecutive points beyond the SAME +/-2 SD limit. --- #
        elif len(zs) >= 2 and self._same_side_beyond(zs[-2:], 2.0):
            rule_violated, status = "2-2S", "FAIL"

        # --- 4-1S: four consecutive points beyond the SAME +/-1 SD limit. -- #
        elif len(zs) >= 4 and self._same_side_beyond(zs[-4:], 1.0):
            rule_violated, status = "4-1S", "FAIL"

        # --- 10x: ten consecutive points on the same side of the mean. ----- #
        elif len(zs) >= 10 and self._same_side(zs[-10:]):
            rule_violated, status = "10x", "FAIL"

        # --- 7T: seven consecutive points trending in one direction. ------- #
        elif len(series) >= 7 and self._trending(series[-7:]):
            rule_violated, status = "7T", "REVIEW_REQUIRED"

        # --- 1-2S: warning only. Does not reject on its own. --------------- #
        elif abs(z_now) > self.WARN_SD:
            rule_violated, status = "1-2S", "REVIEW_REQUIRED"

        return {
            "status": status,
            "rule_violated": rule_violated,
            "rules_checked": list(RULES_CHECKED),
            "mean": target,
            "sd": sd,
            "cv_percent": round(cv_percent, 4),
            "deviation_sd": round(z_now, 4),
        }

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _z(value: float, target: float, sd: float) -> float:
        """Signed deviation of ``value`` from ``target`` in SD units."""
        if sd == 0:
            return 0.0
        return (value - target) / sd

    @staticmethod
    def _same_side_beyond(zs: Sequence[float], limit: float) -> bool:
        """True if every z in ``zs`` is beyond ``+limit`` OR every beyond ``-limit``."""
        if not zs:
            return False
        all_high = all(z > limit for z in zs)
        all_low = all(z < -limit for z in zs)
        return all_high or all_low

    @staticmethod
    def _same_side(zs: Sequence[float]) -> bool:
        """True if every z is strictly on the same side of the mean (0)."""
        if not zs:
            return False
        return all(z > 0 for z in zs) or all(z < 0 for z in zs)

    @staticmethod
    def _trending(values: Sequence[float]) -> bool:
        """True if ``values`` are strictly monotonically increasing or decreasing."""
        if len(values) < 2:
            return False
        increasing = all(b > a for a, b in zip(values, values[1:]))
        decreasing = all(b < a for a, b in zip(values, values[1:]))
        return increasing or decreasing
