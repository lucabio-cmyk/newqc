"""Deterministic test factories for the QConnect-AI wire contract.

These helpers build valid :mod:`shared.models` objects and supporting data
(history series, QConnect-style control limits and HL7 messages) for use across
the unit and cross-project integration suites. Everything is **deterministic**:
any randomness is driven by an explicit seed via :class:`random.Random`, so a
given call always returns the same value. There is no dependence on the wall
clock or the global ``random`` module.

The module is intentionally pure-Python + pydantic only (no numpy/scipy) so it
can be imported anywhere the shared package is installed, including the edge
sync daemon and CI lint jobs.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timezone

from shared.models import (
    AnalyteType,
    QCDataInput,
    QCLevelType,
)

# A fixed timestamp so factory objects never depend on the wall clock. Callers
# that need a different time can override it.
FIXED_TIMESTAMP = datetime(2026, 3, 15, 9, 30, 0, tzinfo=timezone.utc)

# Sensible HCV-AB serology defaults shared by the QC factories.
_DEFAULT_QC: dict = {
    "lab_id": "lab-genova-001",
    "analyzer_id": "ABBOTT-ARCHITECT-001",
    "analyte_code": "HCV-AB",
    "analyte_type": AnalyteType.SEROLOGY,
    "qc_lot_id": "QC-HCV-DIAMEX-202603-001",
    "qc_level": QCLevelType.NORMAL,
    "result_value": 1.45,
    "target_value": 1.50,
    "sd_value": 0.08,
    "operator_id": "EMP00234",
    "timestamp": FIXED_TIMESTAMP,
}


# --------------------------------------------------------------------------- #
# QC input factories
# --------------------------------------------------------------------------- #
def make_qc_input(**overrides) -> QCDataInput:
    """Build a valid :class:`QCDataInput` with HCV-AB serology defaults.

    Any field may be overridden via keyword arguments.

    Args:
        **overrides: field values to override the defaults.

    Returns:
        A validated :class:`QCDataInput` pydantic object.
    """
    data = dict(_DEFAULT_QC)
    data.update(overrides)
    return QCDataInput(**data)


def make_failing_qc_input(**overrides) -> QCDataInput:
    """Build a :class:`QCDataInput` whose value sits well beyond +3 SD.

    With the default target/SD this trips the Westgard ``1-3S`` rule. The value
    is placed at +5 SD so the deviation is unambiguously greater than 3 SD even
    after any rounding.

    Args:
        **overrides: field values to override (applied last, so an explicit
            ``result_value`` wins).

    Returns:
        A validated :class:`QCDataInput` representing a failing QC point.
    """
    base = dict(_DEFAULT_QC)
    target = float(overrides.get("target_value", base["target_value"]))
    sd = float(overrides.get("sd_value", base["sd_value"]))
    base["result_value"] = target + 5.0 * sd  # +5 SD -> definitely > +3 SD
    base.update(overrides)
    return QCDataInput(**base)


# --------------------------------------------------------------------------- #
# History generation (deterministic per seed)
# --------------------------------------------------------------------------- #
def make_qc_history(
    mean: float,
    sd: float,
    n: int = 30,
    *,
    seed: int = 42,
    distribution: str = "gaussian",
) -> list[float]:
    """Generate a deterministic synthetic QC history.

    The series is reproducible for a given ``seed`` because all randomness is
    drawn from a private :class:`random.Random` instance.

    Args:
        mean: central tendency of the series.
        sd: spread of the series (standard deviation for gaussian).
        n: number of points to generate (must be > 0).
        seed: seed for the private RNG (same seed -> same series).
        distribution: one of ``"gaussian"``, ``"bimodal"``, ``"skewed"`` or
            ``"lognormal"``.

    Returns:
        A list of ``n`` floats.

    Raises:
        ValueError: if ``n`` <= 0 or ``distribution`` is unknown.
    """
    if n <= 0:
        raise ValueError("n must be positive")
    rng = random.Random(seed)

    if distribution == "gaussian":
        return [rng.gauss(mean, sd) for _ in range(n)]

    if distribution == "bimodal":
        # Two clusters symmetrically offset from the mean by 2 SD, each with a
        # tighter spread so the two modes are clearly separated.
        offset = 2.0 * sd
        cluster_sd = sd * 0.4
        out: list[float] = []
        for _ in range(n):
            centre = mean - offset if rng.random() < 0.5 else mean + offset
            out.append(rng.gauss(centre, cluster_sd))
        return out

    if distribution == "skewed":
        # Right-skewed via a gamma draw, recentred/rescaled to (mean, sd).
        shape = 2.0
        scale = 1.0
        raw = [rng.gammavariate(shape, scale) for _ in range(n)]
        raw_mean = shape * scale
        raw_sd = math.sqrt(shape) * scale
        return [mean + (x - raw_mean) / raw_sd * sd for x in raw]

    if distribution == "lognormal":
        # Lognormal with sigma derived from the requested CV (sd/mean). Falls
        # back to a modest sigma when mean is non-positive.
        cv = (sd / mean) if mean > 0 else 0.25
        sigma = math.sqrt(math.log(1.0 + cv * cv))
        mu = math.log(mean) - 0.5 * sigma * sigma if mean > 0 else 0.0
        return [math.exp(rng.gauss(mu, sigma)) for _ in range(n)]

    raise ValueError(f"unknown distribution: {distribution!r}")


# --------------------------------------------------------------------------- #
# Control limits
# --------------------------------------------------------------------------- #
def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (matches numpy's default 'linear')."""
    if not values:
        return 0.0
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


def make_control_limits(history: list[float]) -> dict:
    """Build a QConnect-style control-limits dict from a history series.

    The returned dict carries the original ``history`` plus the 5th/95th
    percentiles and the LCL/UCL (aliased to those percentiles), matching the
    keys consumed by ``QConnectEngine.evaluate``.

    Args:
        history: the analyte's historical control values.

    Returns:
        A dict with keys ``history``, ``percentile_5``, ``percentile_95``,
        ``lcl`` and ``ucl``.
    """
    p5 = _percentile(history, 5.0)
    p95 = _percentile(history, 95.0)
    return {
        "history": list(history),
        "percentile_5": p5,
        "percentile_95": p95,
        "lcl": p5,
        "ucl": p95,
    }


# --------------------------------------------------------------------------- #
# HL7
# --------------------------------------------------------------------------- #
def make_hl7_message(
    *,
    analyte: str = "HCV-AB",
    value: float = 1.45,
    control_id: str = "MSG00001",
) -> str:
    """Build a valid HL7 v2.5 ORU^R01 message string (MSH + OBX).

    Segments are CR-separated per the HL7 standard, so the result is parseable
    by ``edge.qc_inference.hl7.parser.HL7Parser.parse_message`` (after optional
    MLLP framing). The single OBX carries the analyte coded as LOINC (``LN``).

    Args:
        analyte: analyte/observation identifier (OBX-3 id component).
        value: numeric observation value (OBX-5).
        control_id: message control id (MSH-10).

    Returns:
        The HL7 message as a CR-delimited string.
    """
    msh = "MSH|^~\\&|ARCHITECT|LAB|QCONNECT|LAB|20260315093000||" f"ORU^R01|{control_id}|P|2.5"
    obr = "OBR|1||ORD123|QC^Quality Control^L"
    obx = f"OBX|1|NM|{analyte}^{analyte}^LN||{value}|S/CO|0.00-1.00|N|||F"
    return "\r".join([msh, obr, obx])
