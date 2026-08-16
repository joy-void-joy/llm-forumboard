"""Folding per-turn usage into a run total, and into the stage that spent it."""

from datetime import timedelta

import pytest

from lup.runtime.models import SessionId, TurnId, TurnIdentifiers
from lup.runtime.usage import CostAccumulator, Spend, per_mtok_usage_cost
from lup.runtime.wrappers import UsageRecord
from lup.types import Usage


def turn(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float | None = None,
    cache_read_input_tokens: int = 0,
    seconds: float = 1.0,
) -> UsageRecord:
    return UsageRecord(
        identifiers=TurnIdentifiers(
            session=SessionId(value="s"), turn=TurnId(value="t")
        ),
        usage=Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            cache_read_input_tokens=cache_read_input_tokens,
        ),
        duration=timedelta(seconds=seconds),
    )


class TestRecord:
    def test_turns_fold_into_the_running_total(self) -> None:
        costs = CostAccumulator()

        costs.record(turn(input_tokens=100, output_tokens=10, cost_usd=0.5))
        costs.record(turn(input_tokens=200, output_tokens=20, cost_usd=0.25))

        assert costs.total.turn_count == 2
        assert costs.total.cost_usd == pytest.approx(0.75)
        assert costs.total.input_tokens == 300
        assert costs.total.total_tokens == 330
        assert costs.total.duration == timedelta(seconds=2)

    def test_an_unstaged_turn_reaches_the_total_only(self) -> None:
        costs = CostAccumulator()

        costs.record(turn(cost_usd=1.0))

        assert costs.total.cost_usd == pytest.approx(1.0)
        assert costs.stages == {}

    def test_a_stage_is_billed_alongside_the_total(self) -> None:
        costs = CostAccumulator()

        costs.record(turn(cost_usd=1.0), stage="research")

        assert costs.total.cost_usd == pytest.approx(1.0)
        assert costs.stages["research"].cost_usd == pytest.approx(1.0)


class TestCostSource:
    def test_the_provider_reported_cost_wins(self) -> None:
        costs = CostAccumulator(
            usage_cost=per_mtok_usage_cost(input_usd=1000.0, output_usd=1000.0)
        )

        costs.record(turn(input_tokens=1_000_000, cost_usd=0.02))

        assert costs.total.cost_usd == pytest.approx(0.02)

    def test_the_estimator_covers_an_adapter_that_reports_none(self) -> None:
        costs = CostAccumulator(
            usage_cost=per_mtok_usage_cost(input_usd=3.0, output_usd=15.0)
        )

        costs.record(turn(input_tokens=1_000_000, output_tokens=1_000_000))

        assert costs.total.cost_usd == pytest.approx(18.0)

    def test_no_cost_and_no_estimator_still_counts_tokens(self) -> None:
        costs = CostAccumulator()

        costs.record(turn(input_tokens=500, output_tokens=5))

        assert costs.total.cost_usd == 0.0
        assert costs.total.total_tokens == 505


class TestSink:
    async def test_two_stages_bill_apart(self) -> None:
        costs = CostAccumulator()

        await costs.sink("research")(turn(cost_usd=1.0, input_tokens=10))
        await costs.sink("write")(turn(cost_usd=2.0, input_tokens=20))

        assert costs.stages["research"].cost_usd == pytest.approx(1.0)
        assert costs.stages["write"].cost_usd == pytest.approx(2.0)
        assert costs.total.cost_usd == pytest.approx(3.0)
        assert costs.total.input_tokens == 30


class TestSnapshot:
    def test_a_dumped_run_restores_to_the_same_spend(self) -> None:
        costs = CostAccumulator()
        costs.record(turn(cost_usd=1.5, input_tokens=40), stage="research")

        restored = CostAccumulator.model_validate(costs.model_dump())

        assert restored.total.cost_usd == pytest.approx(1.5)
        assert restored.stages["research"].input_tokens == 40

    def test_merging_a_snapshot_carries_a_run_across_a_restart(self) -> None:
        before = CostAccumulator()
        before.record(turn(cost_usd=1.0, input_tokens=10), stage="research")

        after = CostAccumulator()
        after.record(turn(cost_usd=2.0, input_tokens=20), stage="research")
        after.merge(CostAccumulator.model_validate(before.model_dump()))

        assert after.total.cost_usd == pytest.approx(3.0)
        assert after.stages["research"].input_tokens == 30
        assert after.stages["research"].turn_count == 2

    def test_merging_brings_over_a_stage_the_live_run_never_saw(self) -> None:
        before = CostAccumulator()
        before.record(turn(cost_usd=1.0), stage="research")

        after = CostAccumulator()
        after.merge(before)

        assert after.stages["research"].cost_usd == pytest.approx(1.0)


class TestSpend:
    def test_merging_adds_every_field(self) -> None:
        spend = Spend(cost_usd=1.0, input_tokens=5, output_tokens=2, turn_count=1)
        spend.merge(Spend(cost_usd=2.0, input_tokens=10, output_tokens=3, turn_count=1))

        assert spend.cost_usd == pytest.approx(3.0)
        assert spend.total_tokens == 20
        assert spend.turn_count == 2
