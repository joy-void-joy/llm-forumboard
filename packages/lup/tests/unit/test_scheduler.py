"""Scheduler timing behavior: sleep/wake and debounce windows.

The persistent-agent scheduler is concurrency-critical: a missed wake
or a stale debounce window silently stalls the agent. These tests use
real (short) timers.
"""

import asyncio

from lup.realtime.scheduler import Scheduler


def make_scheduler() -> Scheduler:
    async def on_action(content: str) -> None:
        del content

    return Scheduler(on_action=on_action)


class TestSleepWake:
    async def test_wake_interrupts_sleep_with_reason(self) -> None:
        scheduler = make_scheduler()

        async def waker() -> None:
            await asyncio.sleep(0.05)
            scheduler.wake("new-message")

        waker_task = asyncio.create_task(waker())
        result = await scheduler.sleep(5)
        await waker_task

        assert result.reason == "new-message"

    async def test_sleep_times_out_with_timer_reason(self) -> None:
        scheduler = make_scheduler()

        result = await scheduler.sleep(0)

        assert result.reason == "timer"

    async def test_pending_wake_returns_immediately_then_clears(self) -> None:
        scheduler = make_scheduler()
        scheduler.wake("early")

        result = await scheduler.sleep(60)

        assert result.reason == "early"
        assert scheduler.wake_pending is False


class TestDebounce:
    async def test_activity_then_quiet_wakes_with_event(self) -> None:
        scheduler = make_scheduler()
        scheduler.start_debounce(initial_seconds=1, quiet_seconds=0)
        scheduler.extend_debounce()

        await asyncio.sleep(0.05)

        assert scheduler.wake_pending
        result = await scheduler.sleep(5)
        assert result.reason == "event"

    async def test_empty_window_wakes_on_timer(self) -> None:
        scheduler = make_scheduler()
        scheduler.start_debounce(initial_seconds=0, quiet_seconds=1)

        await asyncio.sleep(0.05)

        result = await scheduler.sleep(5)
        assert result.reason == "timer"

    async def test_replacing_window_cancels_previous(self) -> None:
        scheduler = make_scheduler()
        scheduler.start_debounce(initial_seconds=60, quiet_seconds=60)
        assert scheduler.debounce_active

        scheduler.start_debounce(
            initial_seconds=0, quiet_seconds=1, wake_on_empty=False
        )
        await asyncio.sleep(0.05)

        assert not scheduler.debounce_active
        assert not scheduler.wake_pending
