"""A spending ceiling that outlives the process holding it.

:class:`~lup.runtime.wrappers.BudgetConfig` caps one logical turn and raises
when the turn would exceed it. This caps a *period* — a rolling allowance such
as "so many dollars a day" — and, like :mod:`lup.runtime.quota`, waits rather
than failing: the run is long-lived and the money comes back when the window
rolls over, so stopping it would abandon work that only needed to be slower.

Two properties follow from the ceiling being an account's rather than a run's.
It is durable, because a run that crashes and restarts must not get a fresh
allowance; and it is shared, because several agents drawing on one account are
spending the same money. Both come from one small locked file the period's
spend is read and charged through, so concurrent processes serialize on it
rather than each keeping a private count.
"""

import asyncio
import fcntl
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from lup.runtime.contracts import Session, Turn
from lup.runtime.factory import SessionFactory
from lup.runtime.models import (
    SessionHandle,
    SessionId,
    TurnHandle,
    TurnRequest,
    TurnResult,
)
from lup.types import UsageCost


class FinancialBudgetConfig(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """One fixed UTC allowance period and where its spend is recorded."""

    maximum_usd: float = Field(gt=0)
    period_seconds: int = Field(default=86_400, gt=0)
    state_path: Path
    usage_cost: UsageCost


class FinancialBudgetState(BaseModel, frozen=True):
    """The durable spend counter for one period."""

    window_start_epoch: int
    spent_usd: float = Field(ge=0)


class FinancialBudgetEvent(BaseModel, frozen=True):
    """Observable charge, sleep, or wake transition."""

    phase: Literal["charge", "sleep", "wake"]
    window_start_epoch: int
    spent_usd: float = Field(ge=0)
    maximum_usd: float = Field(gt=0)
    wait_seconds: float = Field(ge=0)
    charge_usd: float | None = Field(default=None, ge=0)


type BudgetSink = Callable[[FinancialBudgetEvent], Awaitable[None]]
type BudgetSleeper = Callable[[float], Awaitable[None]]
type EpochProvider = Callable[[], float]


def utc_epoch() -> float:
    """Current UTC epoch seconds, the clock periods are cut against."""
    return datetime.now(UTC).timestamp()


class FinancialBudgetStore:
    """The period counter, read and charged under a cross-process lock."""

    def __init__(self, config: FinancialBudgetConfig) -> None:
        self.config = config

    def window_start(self, epoch: float) -> int:
        """The fixed period containing ``epoch``.

        Cut on absolute epoch boundaries rather than from first use, so every
        process sharing the account agrees on which window it is in without
        having to agree on when the run began.
        """
        period = self.config.period_seconds
        return int(epoch // period) * period

    def transact(self, epoch: float, charge_usd: float = 0) -> FinancialBudgetState:
        """Read, roll over, and charge in one locked pass.

        Read-then-write cannot be split: two agents that each read a spend of
        $9 against a $10 ceiling would both conclude they may proceed. Holding
        the lock across the whole transaction is what makes the ceiling mean
        the account rather than each process's view of it.
        """
        path = self.config.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                content = handle.read()
                current_start = self.window_start(epoch)
                recorded = (
                    FinancialBudgetState.model_validate_json(content)
                    if content.strip()
                    else None
                )
                carried = (
                    recorded.spent_usd
                    if recorded is not None
                    and recorded.window_start_epoch == current_start
                    else 0
                )
                state = FinancialBudgetState(
                    window_start_epoch=current_start,
                    spent_usd=carried + charge_usd,
                )
                handle.seek(0)
                handle.truncate()
                handle.write(json.dumps(state.model_dump(mode="json"), sort_keys=True))
                handle.write("\n")
                handle.flush()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return state

    def wait_seconds(self, state: FinancialBudgetState, epoch: float) -> float:
        """Seconds until the next period, with a boundary grace either side."""
        reset = state.window_start_epoch + self.config.period_seconds
        return max(1.0, reset - epoch + 1.0)


class FinancialBudgetTurn[T: BaseModel | None](Turn[T]):
    """Charge a completed turn exactly once, and keep its result.

    A caller may await the same turn twice; the completed result is held so
    the second await returns it rather than charging the account again.
    """

    def __init__(
        self,
        inner: Turn[T],
        store: FinancialBudgetStore,
        sink: BudgetSink,
        now: EpochProvider,
    ) -> None:
        self.inner = inner
        self.store = store
        self.sink = sink
        self.now = now
        self.completed: TurnResult[T] | None = None

    async def result(self) -> TurnResult[T]:
        if self.completed is not None:
            return self.completed
        result = await self.inner.result()
        charge = self.store.config.usage_cost(result.usage)
        state = self.store.transact(self.now(), charge)
        await self.sink(
            FinancialBudgetEvent(
                phase="charge",
                window_start_epoch=state.window_start_epoch,
                spent_usd=state.spent_usd,
                maximum_usd=self.store.config.maximum_usd,
                wait_seconds=0,
                charge_usd=charge,
            )
        )
        self.completed = result
        return result


class FinancialBudgetSession(Session):
    """Hold new work at the door while the period is spent out.

    Checked before starting rather than after charging, because a turn already
    accepted has to be allowed to finish — the overshoot from one final turn is
    charged and carried, and it is the *next* one that waits.
    """

    def __init__(
        self,
        inner: Session,
        store: FinancialBudgetStore,
        sink: BudgetSink,
        sleeper: BudgetSleeper,
        now: EpochProvider,
    ) -> None:
        self.inner = inner
        self.store = store
        self.sink = sink
        self.sleeper = sleeper
        self.now = now

    async def wait_for_allowance(self) -> None:
        """Sleep across exhausted periods without changing model or provider."""
        while True:
            epoch = self.now()
            state = self.store.transact(epoch)
            if state.spent_usd < self.store.config.maximum_usd:
                return
            delay = self.store.wait_seconds(state, epoch)
            event = FinancialBudgetEvent(
                phase="sleep",
                window_start_epoch=state.window_start_epoch,
                spent_usd=state.spent_usd,
                maximum_usd=self.store.config.maximum_usd,
                wait_seconds=delay,
            )
            await self.sink(event)
            await self.sleeper(delay)
            await self.sink(event.model_copy(update={"phase": "wake"}))

    async def start[T: BaseModel | None](
        self, request: TurnRequest[T]
    ) -> TurnHandle[T]:
        await self.wait_for_allowance()
        handle = await self.inner.start(request)
        return TurnHandle[T](
            turn=FinancialBudgetTurn(handle.turn, self.store, self.sink, self.now),
            events=handle.events,
            interrupt=handle.interrupt,
            steer=handle.steer,
        )


# The config is one decorator's settings, and no one of them is the subject:
# what this does is compose a config, a sink and a clock into a factory, which
# belongs to none of the three alone.
# lup: ignore[model-free-function] — composition root wiring a config into a factory
def financial_budget_session_factory(
    inner: SessionFactory,
    config: FinancialBudgetConfig,
    sink: BudgetSink,
    *,
    sleeper: BudgetSleeper = asyncio.sleep,
    now: EpochProvider = utc_epoch,
) -> SessionFactory:
    """Apply one durable period allowance across every session opened."""
    store = FinancialBudgetStore(config)

    @asynccontextmanager
    async def open_budgeted(
        resume: SessionId | None = None,
    ) -> AsyncGenerator[SessionHandle]:
        async with inner.open(resume) as handle:
            yield SessionHandle(
                session=FinancialBudgetSession(
                    handle.session, store, sink, sleeper, now
                ),
                fork=handle.fork,
            )

    return SessionFactory(open_budgeted)
