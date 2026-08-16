"""The durable period allowance: charging, rolling over, and holding at the door."""

from pathlib import Path

import pytest

from lup.runtime.budget import (
    FinancialBudgetConfig,
    FinancialBudgetEvent,
    FinancialBudgetSession,
    FinancialBudgetStore,
)
from lup.runtime.composition import AcceptedTurn, CompletedTurn, ComposedSession
from lup.runtime.models import (
    SessionId,
    TurnId,
    TurnIdentifiers,
    turn_request,
)
from lup.types import Usage
from tests.unit.test_capability_runtime import RecordingBinder

# One dollar a turn, so a ceiling reads as a turn count in these tests.
DOLLAR_A_TURN = 1.0


def config(state_path: Path, maximum_usd: float) -> FinancialBudgetConfig:
    return FinancialBudgetConfig(
        maximum_usd=maximum_usd,
        period_seconds=3600,
        state_path=state_path,
        usage_cost=lambda _usage: DOLLAR_A_TURN,
    )


class Recorder:
    def __init__(self) -> None:
        self.events: list[FinancialBudgetEvent] = []
        self.slept: list[float] = []

    async def sink(self, event: FinancialBudgetEvent) -> None:
        self.events.append(event)

    async def sleeper(self, seconds: float) -> None:
        self.slept.append(seconds)


def budgeted_session(
    settings: FinancialBudgetConfig,
    recorder: Recorder,
    clock: list[float],
) -> FinancialBudgetSession:
    sequence = 0

    async def start(_text: str) -> AcceptedTurn:
        nonlocal sequence
        turn = sequence
        sequence += 1

        async def complete() -> CompletedTurn:
            return CompletedTurn(usage=Usage())

        return AcceptedTurn(
            identifiers=TurnIdentifiers(
                session=SessionId(value="budget"),
                turn=TurnId(value=f"turn-{turn}"),
            ),
            complete=complete,
        )

    return FinancialBudgetSession(
        ComposedSession(start, RecordingBinder()),
        FinancialBudgetStore(settings),
        recorder.sink,
        recorder.sleeper,
        lambda: clock[0],
    )


@pytest.mark.asyncio
async def test_a_completed_turn_is_charged_once_however_often_it_is_awaited(
    tmp_path: Path,
) -> None:
    recorder = Recorder()
    session = budgeted_session(
        config(tmp_path / "spend.json", maximum_usd=10), recorder, [0.0]
    )

    handle = await session.start(turn_request("work"))
    await handle.turn.result()
    await handle.turn.result()

    charges = [event for event in recorder.events if event.phase == "charge"]
    assert [event.spent_usd for event in charges] == [1.0]


@pytest.mark.asyncio
async def test_spend_survives_a_new_store_over_the_same_file(tmp_path: Path) -> None:
    # The ceiling belongs to the account, so a process that dies and restarts
    # must not be handed the period's allowance a second time.
    state_path = tmp_path / "spend.json"
    settings = config(state_path, maximum_usd=10)
    FinancialBudgetStore(settings).transact(0.0, charge_usd=4)

    reopened = FinancialBudgetStore(settings).transact(0.0)

    assert reopened.spent_usd == 4


@pytest.mark.asyncio
async def test_an_exhausted_period_holds_the_next_turn_until_it_rolls(
    tmp_path: Path,
) -> None:
    recorder = Recorder()
    clock = [0.0]
    settings = config(tmp_path / "spend.json", maximum_usd=1)
    session = budgeted_session(settings, recorder, clock)

    first = await session.start(turn_request("work"))
    await first.turn.result()

    # The period is spent. Starting again sleeps, and the sleeper advances the
    # clock past the boundary the way real time would.
    async def rolling(seconds: float) -> None:
        recorder.slept.append(seconds)
        clock[0] += seconds

    session.sleeper = rolling
    second = await session.start(turn_request("work"))
    await second.turn.result()

    assert recorder.slept == [3601.0]
    assert [event.phase for event in recorder.events] == [
        "charge",
        "sleep",
        "wake",
        "charge",
    ]


@pytest.mark.asyncio
async def test_the_roll_over_starts_the_next_period_from_zero(tmp_path: Path) -> None:
    settings = config(tmp_path / "spend.json", maximum_usd=10)
    store = FinancialBudgetStore(settings)
    store.transact(0.0, charge_usd=9)

    assert store.transact(3600.0).spent_usd == 0
