"""The one way anything in this package waits.

Two surfaces observe the same publication: a poll tick bounds how late an
out-of-process write can be noticed, and an optional event lets an
in-process writer deliver one with no delay at all. Both read the same
files, so there is one mechanism rather than a fast path and a slow path
that can disagree about what has happened.
"""

import asyncio
import time
from collections.abc import Callable

POLL_SECONDS = 0.25


async def wait_until[T](
    ready: Callable[[], T | None],
    *,
    wait_seconds: float,
    poll_interval_seconds: float = POLL_SECONDS,
    wake: asyncio.Event | None = None,
) -> T | None:
    """Return what ``ready`` first reports, or ``None`` at the deadline.

    ``ready`` is re-read rather than trusted once, because the whole point
    is that another process may have written since the last look.
    """
    deadline = time.monotonic() + wait_seconds
    while True:
        found = ready()
        if found is not None:
            return found
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        interval = min(poll_interval_seconds, remaining)
        if wake is None:
            await asyncio.sleep(interval)
            continue
        try:
            async with asyncio.timeout(interval):
                await wake.wait()
        except TimeoutError:
            continue
        wake.clear()
