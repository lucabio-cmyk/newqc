"""Exponential backoff helpers for resilient cloud sync.

The edge may be offline for minutes to hours. Rather than hammering the cloud,
the sync daemon backs off exponentially, capped at 30 minutes. The documented
schedule (base=60s) is roughly:

    attempt 0 -> 60s (1 min)
    attempt 1 -> 120s
    attempt 2 -> 240s
    attempt 3 -> 480s
    attempt 4 -> 960s
    attempt 5 -> 1800s (capped at 30 min, stays here)

So the practical cadence is approximately "1 min, then a few minutes, then every
~5-30 min" up to ``max_attempts`` (default 10) before giving up on a cycle and
trying again on the next loop tick. Nothing is lost: pending records remain in
the cache until acknowledged.
"""

from __future__ import annotations

import asyncio
import functools
import random
from typing import Any, Awaitable, Callable, TypeVar

from loguru import logger

T = TypeVar("T")

DEFAULT_BASE_SECONDS = 1.0
DEFAULT_MAX_WAIT_SECONDS = 1800.0  # 30 minutes
DEFAULT_MAX_ATTEMPTS = 10


def compute_backoff(
    attempt: int, base: float = DEFAULT_BASE_SECONDS, max_wait: float = DEFAULT_MAX_WAIT_SECONDS
) -> float:
    """Compute the backoff wait for a given attempt.

    Args:
        attempt: zero-based attempt index.
        base: base delay in seconds.
        max_wait: maximum delay (the cap), in seconds.

    Returns:
        ``min(base * 2**attempt, max_wait)`` (no jitter; see
        :func:`compute_backoff_jitter` for a jittered variant).
    """
    return min(base * (2 ** max(0, attempt)), max_wait)


def compute_backoff_jitter(
    attempt: int,
    base: float = DEFAULT_BASE_SECONDS,
    max_wait: float = DEFAULT_MAX_WAIT_SECONDS,
) -> float:
    """Backoff with full jitter to avoid thundering-herd reconnects."""
    ceiling = compute_backoff(attempt, base, max_wait)
    return random.uniform(0.0, ceiling)


def async_retry(
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base: float = 60.0,
    max_wait: float = DEFAULT_MAX_WAIT_SECONDS,
    *,
    jitter: bool = True,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Decorator: retry an async function with exponential backoff.

    Args:
        max_attempts: total attempts before re-raising the last exception.
        base: base delay in seconds (60s gives the documented 1-min start).
        max_wait: cap on the per-attempt delay.
        jitter: apply full jitter to each wait.
        retry_on: exception types that trigger a retry.

    Returns:
        A decorator wrapping the target coroutine function.
    """

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            last_exc: BaseException | None = None
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except retry_on as exc:  # type: ignore[misc]
                    last_exc = exc
                    if attempt == max_attempts - 1:
                        break
                    wait = (
                        compute_backoff_jitter(attempt, base, max_wait)
                        if jitter
                        else compute_backoff(attempt, base, max_wait)
                    )
                    logger.warning(
                        "{} failed (attempt {}/{}): {} - retrying in {:.0f}s",
                        getattr(func, "__name__", "task"),
                        attempt + 1,
                        max_attempts,
                        exc,
                        wait,
                    )
                    await asyncio.sleep(wait)
            assert last_exc is not None
            logger.error(
                "{} exhausted {} attempts; giving up for this cycle.",
                getattr(func, "__name__", "task"),
                max_attempts,
            )
            raise last_exc

        return wrapper

    return decorator
