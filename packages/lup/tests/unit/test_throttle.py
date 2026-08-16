"""Tests for the Throttle utility."""

import asyncio
import time

import pytest

from lup.resilience.throttle import Throttle


@pytest.mark.asyncio
async def test_concurrency_limit() -> None:
    """Verify max_concurrent limits parallel execution."""
    throttle = Throttle(max_concurrent=2)
    active = 0
    max_active = 0

    async def work() -> None:
        nonlocal active, max_active
        async with throttle.slot():
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.05)
            active -= 1

    await asyncio.gather(*[work() for _ in range(10)])
    assert max_active <= 2


@pytest.mark.asyncio
async def test_min_interval_enforced() -> None:
    """Verify min_interval enforces temporal spacing between requests."""
    throttle = Throttle(max_concurrent=10, min_interval=0.1)
    timestamps: list[float] = []

    async def work() -> None:
        async with throttle.slot():
            timestamps.append(time.monotonic())

    await asyncio.gather(*[work() for _ in range(5)])
    timestamps.sort()
    for i in range(1, len(timestamps)):
        gap = timestamps[i] - timestamps[i - 1]
        assert gap >= 0.09  # Allow small floating-point slack


@pytest.mark.asyncio
async def test_no_interval_is_fast() -> None:
    """With min_interval=0, only concurrency is limited -- no added delay."""
    throttle = Throttle(max_concurrent=5, min_interval=0.0)
    start = time.monotonic()

    async def work() -> None:
        async with throttle.slot():
            pass

    await asyncio.gather(*[work() for _ in range(5)])
    elapsed = time.monotonic() - start
    assert elapsed < 0.1
