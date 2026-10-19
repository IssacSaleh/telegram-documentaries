"""Retry-with-backoff helper (ROADMAP Phase 7 — Resilience).

TECH.md error policy: "Retry with backoff first; if it still fails, send a
friendly message and keep the session alive." These tests pin the shared
helper that policy is built on: transient-only detection, bounded attempts,
capped exponential backoff, an injectable sleeper (tests never really
sleep), and loud structured logging at every retry and at exhaustion.
"""

from __future__ import annotations

import concurrent.futures
import logging

import httpx
import pytest

from app.resilience import (
    RetryPolicy,
    call_with_retries,
    is_transient_error,
)


class GeminiTimeoutError(Exception):
    """Name-based stand-in for an SDK timeout class (no hard SDK coupling)."""


class GeminiDeadlineExceeded(Exception):
    """Name-based stand-in for a deadline-style SDK error."""


class TestRetryPolicyDefaults:
    def test_default_policy_is_bounded(self) -> None:
        policy = RetryPolicy()

        assert policy.max_attempts == 3
        assert policy.base_delay == 0.5
        assert policy.factor == 2.0
        assert policy.max_delay == 4.0

    def test_policy_is_immutable(self) -> None:
        policy = RetryPolicy()

        with pytest.raises(AttributeError):
            policy.max_attempts = 5  # type: ignore[misc]


class TestIsTransientError:
    """Only transient/timeout errors are retried; everything else is not."""

    @pytest.mark.parametrize(
        "exc",
        [
            TimeoutError("deadline exceeded"),
            concurrent.futures.TimeoutError("future timed out"),
            httpx.ReadTimeout("read timed out"),
            httpx.ConnectTimeout("connect timed out"),
            GeminiTimeoutError("gemini timed out"),
            GeminiDeadlineExceeded("attempt deadline passed"),
        ],
        ids=[
            "builtin_timeout",
            "concurrent_futures_timeout",
            "httpx_read_timeout",
            "httpx_connect_timeout",
            "name_timeout",
            "name_deadline",
        ],
    )
    def test_timeout_shapes_are_transient(self, exc: BaseException) -> None:
        assert is_transient_error(exc) is True

    @pytest.mark.parametrize(
        "exc",
        [
            ValueError("bad input"),
            RuntimeError("validation failed"),
            TypeError("wrong shape"),
            KeyError("missing"),
            httpx.HTTPError("generic httpx error"),
        ],
        ids=["value", "runtime", "type", "key", "generic_httpx"],
    )
    def test_deterministic_errors_are_not_transient(self, exc: BaseException) -> None:
        assert is_transient_error(exc) is False

    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    def test_retryable_reported_statuses_are_transient(self, status: int) -> None:
        class StatusError(Exception):
            def __init__(self) -> None:
                self.status_code = status

        assert is_transient_error(StatusError()) is True

    def test_non_retryable_reported_status_is_not_transient(self) -> None:
        class NotFoundError(Exception):
            status_code = 404

        assert is_transient_error(NotFoundError()) is False


class _Recorder:
    """Records every sleep delay so backoff maths is observable, not felt."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


class TestCallWithRetries:
    def test_success_on_the_first_try_returns_immediately(self) -> None:
        recorder = _Recorder()

        result = call_with_retries(lambda: "portrait", sleeper=recorder)

        assert result == "portrait"
        assert recorder.delays == [], "no sleep on success"

    def test_passes_args_and_kwargs_through_to_the_call(self) -> None:
        def add(a: int, *, b: int) -> int:
            return a + b

        assert call_with_retries(add, 1, b=2, sleeper=_Recorder()) == 3

    def test_a_transient_error_is_retried_then_success_returns(self) -> None:
        recorder = _Recorder()
        attempts = {"n": 0}

        def flaky() -> str:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise TimeoutError("gemini timed out")
            return "portrait"

        result = call_with_retries(flaky, sleeper=recorder)

        assert result == "portrait"
        assert attempts["n"] == 3
        assert recorder.delays == [0.5, 1.0], "base * factor**n backoff"

    def test_exhaustion_reraises_the_last_error_unchanged(self) -> None:
        recorder = _Recorder()

        def always_times_out() -> None:
            raise TimeoutError("deadline exceeded")

        with pytest.raises(TimeoutError, match="deadline exceeded"):
            call_with_retries(always_times_out, sleeper=recorder)

        assert recorder.delays == [0.5, 1.0], "one sleep between each pair of tries"

    def test_at_most_max_attempts_calls_are_made(self) -> None:
        attempts = {"n": 0}

        def boom() -> None:
            attempts["n"] += 1
            raise TimeoutError("nope")

        with pytest.raises(TimeoutError):
            call_with_retries(boom, sleeper=_Recorder())

        assert attempts["n"] == RetryPolicy().max_attempts

    def test_a_non_transient_error_reraises_immediately_without_sleep(self) -> None:
        recorder = _Recorder()
        attempts = {"n": 0}

        def invalid() -> None:
            attempts["n"] += 1
            raise ValueError("validation failed")

        with pytest.raises(ValueError, match="validation failed"):
            call_with_retries(invalid, sleeper=recorder)

        assert attempts["n"] == 1, "deterministic errors are never retried"
        assert recorder.delays == []

    def test_delay_is_capped_at_max_delay(self) -> None:
        recorder = _Recorder()
        policy = RetryPolicy(max_attempts=4, base_delay=1.0, factor=10.0, max_delay=4.0)

        def boom() -> None:
            raise TimeoutError("nope")

        with pytest.raises(TimeoutError):
            call_with_retries(boom, policy=policy, sleeper=recorder)

        assert recorder.delays == [1.0, 4.0, 4.0], "delay never exceeds max_delay"

    def test_a_custom_policy_is_honoured(self) -> None:
        recorder = _Recorder()
        attempts = {"n": 0}
        policy = RetryPolicy(max_attempts=5, base_delay=0.1, factor=2.0, max_delay=0.4)

        def flaky() -> str:
            attempts["n"] += 1
            if attempts["n"] < 5:
                raise TimeoutError("nope")
            return "ok"

        assert call_with_retries(flaky, policy=policy, sleeper=recorder) == "ok"
        assert recorder.delays == [0.1, 0.2, 0.4, 0.4], (
            "four failures sleep before the fifth attempt succeeds; "
            "the 0.8s exponential step is capped at max_delay"
        )

    def test_retry_is_logged_with_the_attempt_and_delay(self, caplog) -> None:
        attempts = {"n": 0}

        def flaky() -> str:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise TimeoutError("nope")
            return "ok"

        with caplog.at_level(logging.INFO, logger="telegram_documentaries.resilience"):
            call_with_retries(flaky, sleeper=_Recorder())

        assert any(
            "event=gemini_retry attempt=1" in r.message and "delay=" in r.message
            for r in caplog.records
        ), "each retry must be visible to operators"

    def test_exhaustion_is_logged(self, caplog) -> None:
        def boom() -> None:
            raise TimeoutError("nope")

        with (
            caplog.at_level(logging.INFO, logger="telegram_documentaries.resilience"),
            pytest.raises(TimeoutError),
        ):
            call_with_retries(boom, sleeper=_Recorder())

        assert any(
            "event=gemini_retry_exhausted attempts=3" in r.message
            for r in caplog.records
        ), "giving up must be loud, not silent"

    def test_retry_logs_never_leak_the_exception_message(self, caplog) -> None:
        """Structured logs carry the error TYPE only — never free-form
        exception text that could embed a secret (MISSION non-negotiable)."""

        def boom() -> None:
            raise TimeoutError("token=SECRETVALUE timed out")

        with (
            caplog.at_level(logging.INFO, logger="telegram_documentaries.resilience"),
            pytest.raises(TimeoutError),
        ):
            call_with_retries(boom, sleeper=_Recorder())

        assert "SECRETVALUE" not in caplog.text
