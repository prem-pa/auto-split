"""Shared async retry-with-exponential-backoff helper.

External API calls (Telegram, Gemini, Splitwise) reach out over the network and
occasionally hit transient failures — DNS blips, connect timeouts, upstream 5xx,
rate limits. :func:`retry_async` wraps a coroutine factory and retries it on a
caller-specified set of transient exceptions, backing off exponentially with
full jitter between attempts, so a momentary hiccup doesn't surface to the user.

Design notes:
    * Retries only exceptions the caller explicitly marks — never a blanket
      ``except Exception``. The caller owns the retryability policy.
    * ``exclude`` lets you opt a *subclass* out of an otherwise-retryable base
      (e.g. Telegram's ``BadRequest`` is a ``NetworkError`` subclass but is a
      4xx, not transient).
    * ``should_retry`` is a finer predicate for status-code decisions (e.g.
      retry Gemini 429/5xx but not other 4xx).
    * Idempotency is the caller's responsibility. Do NOT wrap a non-idempotent
      mutation in a retry that can fire *after* the request may have reached the
      server (an ambiguous read-timeout) — you risk a duplicate. For such calls,
      restrict ``retry_on`` to pre-send failures (e.g. connection errors only).
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


async def retry_async(
    func: Callable[[], Awaitable[T]],
    *,
    retry_on: tuple[type[BaseException], ...],
    exclude: tuple[type[BaseException], ...] = (),
    should_retry: Callable[[BaseException], bool] | None = None,
    max_attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    jitter: bool = True,
    name: str = "api call",
) -> T:
    """Call ``func`` (a zero-arg coroutine factory), retrying transient errors.

    Args:
        func: Zero-argument callable returning a *fresh* awaitable each call —
            it's invoked once per attempt, so it must be safely re-callable.
        retry_on: Exception types that trigger a retry. Anything not matching
            propagates immediately.
        exclude: Subtypes of ``retry_on`` that should NOT be retried (re-raised
            even though they'd otherwise match).
        should_retry: Optional predicate; when provided, a caught exception is
            retried only if it returns ``True`` (lets you gate on e.g. HTTP
            status codes).
        max_attempts: Total attempts including the first (>= 1).
        base_delay: First backoff in seconds; doubles each subsequent retry.
        max_delay: Cap on a single backoff sleep (before jitter).
        jitter: Apply full jitter — sleep a random duration in ``[0, delay]``.
        name: Label used in retry log lines.

    Returns:
        Whatever ``func`` returns on the first successful attempt.

    Raises:
        The exception from the final failed attempt (or the first
        non-retryable one).
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await func()
        except retry_on as exc:
            non_retryable = isinstance(exc, exclude) or (
                should_retry is not None and not should_retry(exc)
            )
            if non_retryable or attempt >= max_attempts:
                raise
            last_exc = exc
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            if jitter:
                delay = random.uniform(0, delay)
            log.warning(
                "%s failed (attempt %d/%d), retrying in %.2fs: %s: %s",
                name,
                attempt,
                max_attempts,
                delay,
                type(exc).__name__,
                exc,
            )
            await asyncio.sleep(delay)

    # Unreachable: the loop either returns, or raises on the final attempt.
    assert last_exc is not None
    raise last_exc


__all__ = ["retry_async"]
