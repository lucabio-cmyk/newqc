"""Lightweight statistics & helper utilities (pure-Python, no numpy dependency).

These are deliberately dependency-free so the edge image stays small and the
helpers can run inside the sync daemon and tests without scientific stacks.
The cloud services may still prefer numpy/scipy for heavy work.
"""

from __future__ import annotations

import math
import uuid
from typing import Iterable, Sequence


def new_correlation_id() -> str:
    """Generate a fresh correlation id for request tracing."""
    return uuid.uuid4().hex


def mean(values: Sequence[float]) -> float:
    """Arithmetic mean. Returns 0.0 for an empty sequence."""
    if not values:
        return 0.0
    return sum(values) / len(values)


def stddev(values: Sequence[float], *, sample: bool = True) -> float:
    """Standard deviation.

    Args:
        values: numeric sequence.
        sample: use the sample (n-1) denominator when True, population (n) otherwise.
    """
    n = len(values)
    if n < 2:
        return 0.0
    mu = mean(values)
    ss = sum((x - mu) ** 2 for x in values)
    denom = (n - 1) if sample else n
    return math.sqrt(ss / denom)


def cv_percent(values: Sequence[float]) -> float:
    """Coefficient of variation as a percentage (sd / mean * 100)."""
    mu = mean(values)
    if mu == 0:
        return 0.0
    return stddev(values) / mu * 100.0


def z_score(value: float, target: float, sd: float) -> float:
    """Signed deviation in standard-deviation units."""
    if sd == 0:
        return 0.0
    return (value - target) / sd


def percentile(values: Sequence[float], pct: float) -> float:
    """Linear-interpolation percentile (matches numpy's default 'linear' method).

    Args:
        values: numeric sequence (need not be sorted).
        pct: percentile in the inclusive range [0, 100].
    """
    if not values:
        return 0.0
    if not 0 <= pct <= 100:
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
    return ordered[low] * (1 - frac) + ordered[high] * frac


def empirical_cdf_position(value: float, history: Sequence[float]) -> float:
    """Fraction of historical observations <= ``value`` (empirical CDF)."""
    if not history:
        return 0.5
    return sum(1 for x in history if x <= value) / len(history)


def clamp(value: float, low: float, high: float) -> float:
    """Clamp ``value`` into the closed interval ``[low, high]``."""
    return max(low, min(high, value))


def chunked(items: Sequence, size: int) -> Iterable[Sequence]:
    """Yield successive ``size``-length chunks from ``items``."""
    if size <= 0:
        raise ValueError("size must be positive")
    for i in range(0, len(items), size):
        yield items[i : i + size]


def exponential_backoff(attempt: int, base: float, max_wait: float) -> float:
    """Compute exponential backoff wait time: ``min(base * 2**attempt, max_wait)``."""
    return min(base * (2 ** max(0, attempt)), max_wait)
