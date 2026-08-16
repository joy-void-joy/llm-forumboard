"""The shared blocking-call executor: results, failures, and context."""

import contextvars
import threading
from functools import partial

from lup.runtime.threads import run_sync

CURRENT_SPAN: contextvars.ContextVar[str] = contextvars.ContextVar("current_span")


async def test_result_comes_back_from_the_worker_thread() -> None:
    def work(left: int, right: int) -> int:
        return left + right

    assert await run_sync(partial(work, 2, right=3)) == 5


async def test_a_caller_may_widen_the_wake_interval() -> None:
    # Nothing about the result changes; the knob only decides how soon the
    # awaiting task notices, so a long call need not poll every 10ms.
    assert await run_sync(lambda: "done", wake_interval=0.001) == "done"


async def test_the_call_runs_off_the_awaiting_thread() -> None:
    # The point of the executor is that the loop's thread stays free while a
    # blocking call runs, so the work must not land back on it.
    caller = threading.get_ident()
    assert await run_sync(threading.get_ident) != caller


async def test_the_callers_context_reaches_the_call() -> None:
    # A blocking call reads the same context the awaiting side had — a trace's
    # current span resolves in the worker rather than coming back unset.
    CURRENT_SPAN.set("turn-7")
    assert await run_sync(CURRENT_SPAN.get) == "turn-7"


async def test_a_raising_call_raises_where_it_was_awaited() -> None:
    def fails() -> None:
        raise ValueError("the blocking call failed")

    try:
        await run_sync(fails)
    except ValueError as error:
        assert str(error) == "the blocking call failed"
    else:
        raise AssertionError("the failure did not surface at the await")
