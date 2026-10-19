"""Retry-with-backoff for Gemini calls (ROADMAP Phase 7 — Resilience).

TECH.md error policy: on a validated user's conversation path, "Retry with
backoff first; if it still fails, send a friendly message and keep the
session alive." This module is the single shared implementation of that
policy, applied at the five concrete Gemini boundaries (Bouncer
classification, Interviewer turns, Converter generation, Scripter,
Narrator synthesis) — never in the gateway, whose per-stage degrade paths
already turn an exhausted retry into a friendly reply.

Design rules:

* **Transient only.** Deterministic errors (validation, bad request) are
  re-raised immediately — retrying them wastes the long-poll loop's time.
  Transient detection is name-based (``timeout``/``deadline`` substrings,
  retryable HTTP statuses) rather than hard-coupled to SDK exception
  classes that differ across versions.
* **Bounded.** At most ``max_attempts`` calls and a ``max_delay`` cap on
  every sleep, so the synchronous poll loop is never blocked indefinitely.
* **Injectable sleeper.** Tests never really sleep.
* **Loud, secret-safe logs.** ``event=gemini_retry`` per retry and
  ``event=gemini_retry_exhausted`` at give-up; only the error *type* is
  logged, never the free-form exception text (it could embed a secret).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from app.logging_utils import get_logger

logger = get_logger("resilience")

T = TypeVar("T")

#: HTTP statuses worth another attempt (rate limit + transient server errors).
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded exponential backoff. Immutable: one shared policy, no tweaks."""

    max_attempts: int = 3
    base_delay: float = 0.5
    factor: float = 2.0
    max_delay: float = 4.0


def _reported_status(exc: BaseException) -> int | None:
    """An integer HTTP-ish status the exception carries, if any.

    Checks the attribute names SDKs actually use (``status_code``,
    ``code``, ``status``) and ``httpx.HTTPStatusError``'s
    ``response.status_code``. Never inspects the message.
    """
    for attribute in ("status_code", "code", "status"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def is_transient_error(exc: BaseException) -> bool:
    """True for errors worth retrying: timeouts and transient HTTP statuses.

    Name-based on purpose — the Gemini SDK's exception classes differ across
    versions, so a hard isinstance chain would silently stop matching after
    an upgrade and lose the retry. Deterministic failures (validation,
    parsing, bad request) return False and propagate immediately.
    """
    name = type(exc).__name__.lower()
    if "timeout" in name or "deadline" in name:
        return True
    status = _reported_status(exc)
    return status is not None and status in RETRYABLE_STATUSES


#: The shared default policy (frozen, so safe to alias). Module-level to
#: keep the ``policy=`` default B008-clean while reading like the contract.
DEFAULT_POLICY = RetryPolicy()


def call_with_retries(
    call: Callable[..., T],
    *args: Any,
    policy: RetryPolicy = DEFAULT_POLICY,
    sleeper: Callable[[float], None] = time.sleep,
    is_retryable: Callable[[BaseException], bool] = is_transient_error,
    **kwargs: Any,
) -> T:
    """Call ``call(*args, **kwargs)``, retrying transient errors with backoff.

    The delay before retry ``n`` (0-based) is
    ``min(base_delay * factor**n, max_delay)``. Once ``policy.max_attempts``
    calls have been made, the last error is re-raised **unchanged** so the
    caller's existing degrade path (Bouncer fallback, ``RETRY_PROMPT``,
    Converter/Scripter/Narrator fallbacks) notifies the user and keeps the
    session alive. Non-retryable errors re-raise on the first attempt.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return call(*args, **kwargs)
        except Exception as exc:
            if not is_retryable(exc):
                raise
            if attempt >= policy.max_attempts:
                logger.error(
                    "event=gemini_retry_exhausted attempts=%d error=%s",
                    attempt,
                    type(exc).__name__,
                )
                raise
            delay = min(
                policy.base_delay * policy.factor ** (attempt - 1), policy.max_delay
            )
            logger.info(
                "event=gemini_retry attempt=%d delay=%.2f error=%s",
                attempt,
                delay,
                type(exc).__name__,
            )
            sleeper(delay)
