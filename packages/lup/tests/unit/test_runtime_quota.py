"""Waiting out a provider allowance: how long, and what runs afterwards."""

from datetime import UTC, datetime, timedelta

import pytest

from lup.runtime.composition import AcceptedTurn, CompletedTurn, ComposedSession
from lup.runtime.errors import QuotaExceededError, TurnFailure
from lup.runtime.models import (
    SessionId,
    TurnId,
    TurnIdentifiers,
    turn_request,
)
from lup.runtime.quota import (
    QuotaWaitConfig,
    QuotaWaitEvent,
    QuotaWaitingSession,
)
from tests.unit.test_capability_runtime import RecordingBinder

FROZEN_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def exhausted(reset_at: datetime | None) -> QuotaExceededError:
    return QuotaExceededError(
        TurnFailure(message="allowance exhausted"),
        reset_at=reset_at,
        quota_type="five_hour",
    )


class Recorder:
    """Collects the emitted transitions and the sleeps that were asked for."""

    def __init__(self) -> None:
        self.events: list[QuotaWaitEvent] = []
        self.slept: list[float] = []

    async def sink(self, event: QuotaWaitEvent) -> None:
        self.events.append(event)

    async def sleeper(self, seconds: float) -> None:
        self.slept.append(seconds)


def waiting_session(
    starts: list[str],
    failures: list[QuotaExceededError | None],
    recorder: Recorder,
    config: QuotaWaitConfig,
) -> QuotaWaitingSession:
    sequence = 0

    async def start(text: str) -> AcceptedTurn:
        nonlocal sequence
        starts.append(text)
        attempt = sequence
        sequence += 1

        async def complete() -> CompletedTurn:
            failure = failures[attempt]
            if failure is not None:
                raise failure
            return CompletedTurn()

        return AcceptedTurn(
            identifiers=TurnIdentifiers(
                session=SessionId(value="quota"),
                turn=TurnId(value=f"turn-{attempt}"),
            ),
            complete=complete,
        )

    return QuotaWaitingSession(
        ComposedSession(start, RecordingBinder()),
        config,
        recorder.sink,
        recorder.sleeper,
        lambda: FROZEN_NOW,
    )


@pytest.mark.asyncio
async def test_it_sleeps_to_the_stated_reset_then_reruns_the_same_request() -> None:
    # The work is still wanted, so the retry has to be the identical prompt on
    # the identical session — not a fresh or degraded one.
    recorder = Recorder()
    starts: list[str] = []
    session = waiting_session(
        starts,
        [exhausted(FROZEN_NOW + timedelta(seconds=600)), None],
        recorder,
        QuotaWaitConfig(profile="research", reset_grace_seconds=5),
    )

    handle = await session.start(turn_request("prove the bound"))
    await handle.turn.result()

    assert recorder.slept == [605]
    assert starts == ["prove the bound", "prove the bound"]
    assert [event.phase for event in recorder.events] == ["sleep", "wake"]
    assert recorder.events[0].profile == "research"
    assert recorder.events[0].quota_type == "five_hour"


@pytest.mark.asyncio
async def test_a_reset_already_past_still_waits_the_floor() -> None:
    # A clock skewed against the provider's would otherwise retry at once and
    # be refused again immediately.
    recorder = Recorder()
    session = waiting_session(
        [],
        [exhausted(FROZEN_NOW - timedelta(hours=1)), None],
        recorder,
        QuotaWaitConfig(minimum_wait_seconds=30),
    )

    handle = await session.start(turn_request("retry me"))
    await handle.turn.result()

    assert recorder.slept == [30]


@pytest.mark.asyncio
async def test_without_a_stated_reset_it_falls_back_to_its_own_interval() -> None:
    # A 429 carrying no reset says only that the allowance is gone, so the
    # waiter uses its configured interval rather than inventing a time.
    recorder = Recorder()
    session = waiting_session(
        [],
        [exhausted(None), None],
        recorder,
        QuotaWaitConfig(unknown_reset_wait_seconds=300),
    )

    handle = await session.start(turn_request("retry me"))
    await handle.turn.result()

    assert recorder.slept == [300]


@pytest.mark.asyncio
async def test_it_keeps_waiting_across_repeated_exhaustion() -> None:
    recorder = Recorder()
    session = waiting_session(
        [],
        [exhausted(None), exhausted(None), None],
        recorder,
        QuotaWaitConfig(unknown_reset_wait_seconds=120),
    )

    handle = await session.start(turn_request("retry me"))
    await handle.turn.result()

    assert recorder.slept == [120, 120]
    assert [event.phase for event in recorder.events] == [
        "sleep",
        "wake",
        "sleep",
        "wake",
    ]
