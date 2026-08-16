"""Portable usage arithmetic and pricing, independent from native adapters."""

from datetime import timedelta

from pydantic import BaseModel, Field

from lup.runtime.wrappers import UsageRecord, UsageSink
from lup.types import Usage, UsageCost


def per_mtok_usage_cost(
    *,
    input_usd: float,
    output_usd: float,
    cached_input_usd: float | None = None,
) -> UsageCost:
    """Build a usage estimator from prices per million tokens."""

    def cost(usage: Usage) -> float:
        cached = usage.cache_read_input_tokens
        uncached = max(usage.input_tokens - cached, 0)
        cached_rate = input_usd if cached_input_usd is None else cached_input_usd
        return (
            uncached * input_usd
            + cached * cached_rate
            + usage.output_tokens * output_usd
        ) / 1_000_000

    return cost


class Spend(BaseModel):
    """What some span of work has cost so far.

    One shape serves the run's total and each stage beneath it, so a caller
    reads a stage the same way it reads the whole.
    """

    cost_usd: float = 0.0
    duration: timedelta = timedelta()
    input_tokens: int = 0
    output_tokens: int = 0
    turn_count: int = 0

    @property
    def total_tokens(self) -> int:
        """Prompt and completion tokens together."""
        return self.input_tokens + self.output_tokens

    def add(self, entry: UsageRecord, cost_usd: float) -> None:
        """Fold one completed turn into this running total."""
        self.turn_count += 1
        self.cost_usd += cost_usd
        self.duration += entry.duration
        self.input_tokens += entry.usage.input_tokens
        self.output_tokens += entry.usage.output_tokens

    def merge(self, other: "Spend") -> None:
        """Fold another span's spend into this one."""
        self.turn_count += other.turn_count
        self.cost_usd += other.cost_usd
        self.duration += other.duration
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens


class CostAccumulator(BaseModel, arbitrary_types_allowed=True):
    """Cumulative spend across turns, and the per-stage breakdown beneath it.

    A session reports usage one turn at a time through ``UsageConfig``, which
    answers what a turn cost but not what a run has. Hand :meth:`sink` to the
    stage's ``UsageConfig`` and every completed turn folds into both the total
    and that stage.

    Being a model rather than a bespoke class, a snapshot is ``model_dump()``
    and restoring one is ``model_validate()``; :meth:`merge` adds a restored
    snapshot to a live accumulator so a run survives a restart.
    """

    total: Spend = Field(default_factory=Spend)
    stages: dict[str, Spend] = {}
    usage_cost: UsageCost | None = Field(
        default=None,
        description=(
            "Estimator for adapters that report tokens but no cost; unused"
            " when the provider reports one"
        ),
    )

    def record(self, entry: UsageRecord, stage: str | None = None) -> None:
        """Fold one completed turn into the total, and into its stage."""
        reported = entry.usage.cost_usd
        if reported is None and self.usage_cost is not None:
            reported = self.usage_cost(entry.usage)

        self.total.add(entry, reported or 0.0)
        if stage is not None:
            self.stages.setdefault(stage, Spend()).add(entry, reported or 0.0)

    def sink(self, stage: str | None = None) -> UsageSink:
        """A ``UsageConfig`` sink that folds each turn in under ``stage``.

        Binding the name here rather than reading it from ambient state is
        what lets two stages run concurrently and still bill apart.
        """

        async def fold(entry: UsageRecord) -> None:
            self.record(entry, stage)

        return fold

    def merge(self, other: "CostAccumulator") -> None:
        """Add a restored snapshot's spend into this accumulator."""
        self.total.merge(other.total)
        for name, spend in other.stages.items():
            self.stages.setdefault(name, Spend()).merge(spend)
