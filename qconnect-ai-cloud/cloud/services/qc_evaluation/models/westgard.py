"""Westgard multirule QC engine (pure-Python, numpy-optional).

The Westgard rules are the de-facto standard for internal QC in clinical
chemistry. They assume the control values are approximately **Gaussian** around
an assigned mean. Each rule maps a pattern of standard-deviation excursions to a
random or systematic error mode.

Worked example — high-sensitivity Troponin-I
--------------------------------------------
A cardiac lab runs a normal-level Troponin-I control with an assigned
``mean = 0.040 ng/mL`` and ``sd = 0.004 ng/mL``. Over the last two days the
analyzer produced::

    history = [0.040, 0.041, 0.039, 0.042, 0.038, 0.040, 0.043]
    new value = 0.053 ng/mL

The deviation is ``(0.053 - 0.040) / 0.004 = +3.25 SD`` which trips ``1-3S`` —
a random-error rejection. Because Troponin drives ACS rule-in/rule-out
decisions, a false-low control here is clinically dangerous, so the engine
returns ``FAIL`` and the orchestrator escalates severity to CRITICAL.

If instead the last four values had all sat between ``+1SD`` and ``+2SD`` on the
same side, ``4-1S`` would fire (systematic positive bias / calibration drift)
even though no single point breached ``3SD``.

Notes
-----
* All rules operate on the *signed* z-scores ``(x - mean) / sd``.
* ``mean``/``sd`` may be supplied (assigned lot values) or computed from
  history via :meth:`WestgardEngine.calculate_stats`.
* numpy is used when available for the stats helpers but a pure-Python path is
  always present so unit tests run without a scientific stack.
"""

from __future__ import annotations

import math
from typing import Sequence

from loguru import logger

try:  # numpy is optional — engines must work without it.
    import numpy as _np  # type: ignore

    _HAS_NUMPY = True
except Exception:  # pragma: no cover - environment without numpy
    _np = None  # type: ignore
    _HAS_NUMPY = False


class WestgardEngine:
    """Evaluate a QC value against the classic Westgard multirules.

    The engine is stateless: each :meth:`evaluate` call is given the assigned
    mean/sd (or computes them) plus the recent ``history`` needed for the
    multi-point rules (2-2S, R-4S, 4-1S, 10x, 7T).
    """

    #: Canonical ordering, most-specific (rejection) first.
    RULES: tuple[str, ...] = (
        "1-3S",
        "2-2S",
        "R-4S",
        "4-1S",
        "10x",
        "7T",
        "1-2S",
    )

    def __init__(self) -> None:
        self._log = logger.bind(engine="westgard")

    # ------------------------------------------------------------------ #
    # Statistics
    # ------------------------------------------------------------------ #
    def calculate_stats(self, history: Sequence[float]) -> dict[str, float]:
        """Compute ``{mean, sd, cv}`` for a history of control values.

        Uses the sample (n-1) standard deviation. Returns zeros for fewer than
        two observations.
        """
        values = [float(x) for x in history]
        n = len(values)
        if n == 0:
            return {"mean": 0.0, "sd": 0.0, "cv": 0.0}
        if _HAS_NUMPY:
            arr = _np.asarray(values, dtype=float)
            mean = float(arr.mean())
            sd = float(arr.std(ddof=1)) if n > 1 else 0.0
        else:
            mean = sum(values) / n
            if n > 1:
                ss = sum((x - mean) ** 2 for x in values)
                sd = math.sqrt(ss / (n - 1))
            else:
                sd = 0.0
        cv = (sd / mean * 100.0) if mean else 0.0
        return {"mean": mean, "sd": sd, "cv": cv}

    @staticmethod
    def _z(value: float, mean: float, sd: float) -> float:
        """Signed deviation in SD units; 0.0 when ``sd`` is zero."""
        if sd == 0:
            return 0.0
        return (value - mean) / sd

    # ------------------------------------------------------------------ #
    # Individual rules — each returns True when the rule is VIOLATED.
    # ------------------------------------------------------------------ #
    def check_1_2s(self, z: float) -> bool:
        """1-2S (warning): one value beyond +/-2 SD.

        Not a rejection rule on its own; it *gates* the rejection rules. About
        ~5% of in-control results trip it by chance, so using it as a hard
        reject inflates the false-rejection rate.
        """
        return abs(z) > 2.0

    def check_1_3s(self, z: float) -> bool:
        """1-3S (reject): one value beyond +/-3 SD -> random error."""
        return abs(z) > 3.0

    def check_2_2s(self, zs: Sequence[float]) -> bool:
        """2-2S (reject): two consecutive values beyond the same +/-2 SD limit.

        Indicates **systematic** error (bias / calibration shift). Requires the
        current value and the immediately preceding one on the *same* side.
        """
        if len(zs) < 2:
            return False
        a, b = zs[-1], zs[-2]
        return (a > 2.0 and b > 2.0) or (a < -2.0 and b < -2.0)

    def check_r_4s(self, zs: Sequence[float]) -> bool:
        """R-4S (reject): one value beyond +2SD and the other beyond -2SD.

        One above +2SD and the other below -2SD -> **random** error / imprecision.
        The two opposite-side points span >=4SD by construction. A plain
        ``|a-b| >= 4`` check is wrong: a same-side pair can only reach a 4SD
        range if one point already breaches 1-3S, so range-only would mis-fire on
        pairs like (+0.5, +4.5).
        """
        if len(zs) < 2:
            return False
        a, b = zs[-1], zs[-2]
        return (a > 2.0 and b < -2.0) or (a < -2.0 and b > 2.0)

    def check_4_1s(self, zs: Sequence[float]) -> bool:
        """4-1S (reject): four consecutive values beyond the same +/-1 SD limit.

        Systematic error — sustained bias smaller than 2SD that classic single
        limits miss.
        """
        if len(zs) < 4:
            return False
        last4 = zs[-4:]
        return all(z > 1.0 for z in last4) or all(z < -1.0 for z in last4)

    def check_10x(self, zs: Sequence[float]) -> bool:
        """10x (reject): ten consecutive values on the same side of the mean.

        Systematic shift even if every point is within +/-1 SD.
        """
        if len(zs) < 10:
            return False
        last10 = zs[-10:]
        return all(z > 0 for z in last10) or all(z < 0 for z in last10)

    def check_7t(self, zs: Sequence[float]) -> bool:
        """7T (reject): seven consecutive values trending monotonically.

        Detects drift (reagent ageing, electrode fouling) regardless of which
        side of the mean the points sit on.
        """
        if len(zs) < 7:
            return False
        last7 = zs[-7:]
        rising = all(last7[i] < last7[i + 1] for i in range(len(last7) - 1))
        falling = all(last7[i] > last7[i + 1] for i in range(len(last7) - 1))
        return rising or falling

    # ------------------------------------------------------------------ #
    # Orchestration
    # ------------------------------------------------------------------ #
    def evaluate(
        self,
        value: float,
        target: float,
        sd: float,
        history: Sequence[float] | None = None,
    ) -> dict:
        """Run the full multirule cascade for a single new ``value``.

        Args:
            value: the new QC measurement.
            target: assigned mean for the lot/level.
            sd: assigned standard deviation for the lot/level (> 0).
            history: previous control values (oldest -> newest), excluding
                ``value``. Used for the multi-point rules.

        Returns:
            A dict matching :class:`schemas.WestgardResult`::

                {status, rule_violated, rules_checked, mean, sd, cv_percent,
                 deviation_sd}

            ``status`` is one of ``"PASS"``, ``"FAIL"`` (any rejection rule) or
            ``"REVIEW_REQUIRED"`` (only the 1-2S warning tripped).
        """
        hist = [float(x) for x in (history or [])]
        # Series of z-scores including the new value, computed against the
        # assigned target/sd (not the rolling mean — Westgard uses fixed limits).
        series = hist + [float(value)]
        zs = [self._z(x, target, sd) for x in series]
        z_new = zs[-1]

        cv_percent = (sd / target * 100.0) if target else 0.0
        rules_checked = list(self.RULES)

        # Rejection rules first (most clinically significant ordering).
        rule_violated: str | None = None
        if self.check_1_3s(z_new):
            rule_violated = "1-3S"
        elif self.check_2_2s(zs):
            rule_violated = "2-2S"
        elif self.check_r_4s(zs):
            rule_violated = "R-4S"
        elif self.check_4_1s(zs):
            rule_violated = "4-1S"
        elif self.check_10x(zs):
            rule_violated = "10x"
        elif self.check_7t(zs):
            rule_violated = "7T"

        if rule_violated is not None:
            status = "FAIL"
        elif self.check_1_2s(z_new):
            # Warning only — flag for human review, do not auto-reject.
            status = "REVIEW_REQUIRED"
            rule_violated = "1-2S"
        else:
            status = "PASS"

        self._log.debug(
            "westgard evaluated",
            value=value,
            z=round(z_new, 3),
            status=status,
            rule=rule_violated,
        )

        return {
            "status": status,
            "rule_violated": rule_violated,
            "rules_checked": rules_checked,
            "mean": target,
            "sd": sd,
            "cv_percent": cv_percent,
            "deviation_sd": z_new,
        }

    # ------------------------------------------------------------------ #
    # Visualisation data
    # ------------------------------------------------------------------ #
    def plot_levey_jennings(
        self,
        history: Sequence[float],
        target: float,
        sd: float,
    ) -> dict:
        """Produce the data series for a Levey-Jennings chart.

        Returns a JSON-serialisable dict with the per-point z-scores and the
        horizontal reference lines at the mean and +/-1/2/3 SD. The frontend
        renders the actual chart.
        """
        points = [
            {
                "index": i,
                "value": float(v),
                "z": self._z(float(v), target, sd),
            }
            for i, v in enumerate(history)
        ]
        return {
            "points": points,
            "mean": target,
            "lines": {
                "mean": target,
                "plus_1sd": target + sd,
                "minus_1sd": target - sd,
                "plus_2sd": target + 2 * sd,
                "minus_2sd": target - 2 * sd,
                "plus_3sd": target + 3 * sd,
                "minus_3sd": target - 3 * sd,
            },
            "sd": sd,
        }
