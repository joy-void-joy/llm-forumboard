"""Retry decorator behavior: what retries, what doesn't, what gives up."""

import httpx
import pytest

from lup.resilience.retry import with_retry


class TestWithRetry:
    async def test_retries_transient_errors_then_succeeds(self) -> None:
        calls = 0

        @with_retry(max_attempts=3, min_wait=0, max_wait=0)
        async def flaky() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise httpx.ConnectError("connection refused")
            return "ok"

        assert await flaky() == "ok"
        assert calls == 3

    async def test_non_retryable_errors_raise_immediately(self) -> None:
        calls = 0

        @with_retry(max_attempts=3, min_wait=0, max_wait=0)
        async def broken() -> str:
            nonlocal calls
            calls += 1
            raise ValueError("logic bug — retrying would hide it")

        with pytest.raises(ValueError):
            await broken()
        assert calls == 1

    async def test_exhausted_attempts_reraise_original_error(self) -> None:
        @with_retry(max_attempts=2, min_wait=0, max_wait=0)
        async def always_down() -> str:
            raise httpx.ConnectError("still down")

        with pytest.raises(httpx.ConnectError):
            await always_down()

    async def test_extra_exceptions_become_retryable(self) -> None:
        calls = 0

        @with_retry(
            max_attempts=2, min_wait=0, max_wait=0, extra_exceptions=(KeyError,)
        )
        async def keyed() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise KeyError("transient")
            return "ok"

        assert await keyed() == "ok"
        assert calls == 2
