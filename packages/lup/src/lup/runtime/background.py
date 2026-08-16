"""Debounced background turns on one persistent session.

A runtime-level consumer of the session capabilities: callers push
replaceable typed state, wakes inside the debounce window coalesce, and each
surviving wake becomes one turn on a single long-lived session. Distinct
from :mod:`lup.realtime`, which owns a persistent agent's full sleep/wake
lifecycle (scheduler state machine, reminders, subprocess relay) and sits a
layer above this engine.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, Field

from lup.runtime.factory import SessionFactory
from lup.runtime.errors import TurnError
from lup.runtime.models import TurnRequest, TurnResult

logger = logging.getLogger(__name__)


class BackgroundConfig(BaseModel, frozen=True):
    """Scheduling behavior independent from provider construction."""

    debounce_seconds: float = Field(default=0.1, ge=0)


type StateToRequest[S: BaseModel, T: BaseModel | None] = Callable[[S], TurnRequest[T]]
type BackgroundResultHandler[T: BaseModel | None] = Callable[
    [TurnResult[T]], Awaitable[None]
]
type BackgroundErrorHandler = Callable[[TurnError], Awaitable[None]]


class BackgroundAgent[S: BaseModel, T: BaseModel | None]:
    """Coalesce wakes and execute the latest typed state in one persistent session."""

    def __init__(
        self,
        factory: SessionFactory,
        state_to_request: StateToRequest[S, T],
        result_handler: BackgroundResultHandler[T],
        error_handler: BackgroundErrorHandler,
        config: BackgroundConfig | None = None,
    ) -> None:
        self.factory = factory
        self.state_to_request = state_to_request
        self.result_handler = result_handler
        self.error_handler = error_handler
        self.config = config or BackgroundConfig()
        self.changed = asyncio.Event()
        self.pending: S | None = None
        self.task: asyncio.Task[None] | None = None
        self.stopping = False

    async def start(self) -> None:
        """Start one scheduler task; provider resources remain lazily opened there."""
        if self.task is not None and not self.task.done():
            raise RuntimeError("background agent is already started")
        self.stopping = False
        self.task = asyncio.create_task(self.run())

    def wake(self, state: S) -> None:
        """Replace pending state and wake the debounce loop."""
        if self.stopping:
            raise RuntimeError("background agent is stopping")
        self.pending = state.model_copy(deep=True)
        self.changed.set()

    async def stop(self) -> None:
        """Stop scheduling and abort an unfinished native turn on context exit."""
        self.stopping = True
        self.changed.set()
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                logger.debug("background agent scheduler stopped by cancellation")
            self.task = None

    async def run(self) -> None:
        async with self.factory.open() as handle:
            while not self.stopping:
                await self.changed.wait()
                self.changed.clear()
                if self.stopping:
                    break
                await self.debounce()
                state = self.pending
                self.pending = None
                if state is None:
                    continue
                request = self.state_to_request(state)
                try:
                    turn = await handle.session.start(request)
                    result = await turn.turn.result()
                except TurnError as error:
                    await self.error_handler(error)
                else:
                    await self.result_handler(result)

    async def debounce(self) -> None:
        """Restart the window while additional wakes arrive."""
        while self.config.debounce_seconds > 0:
            try:
                await asyncio.wait_for(
                    self.changed.wait(), timeout=self.config.debounce_seconds
                )
            except TimeoutError:
                return
            self.changed.clear()
            if self.stopping:
                return
