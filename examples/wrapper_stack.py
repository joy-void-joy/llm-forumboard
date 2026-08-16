"""Compose timeout, budget, retry, correction, persistence, and serialization."""

import asyncio
from pathlib import Path

from lup.adapters.claude.runtime import (
    ClaudeSessionConfig,
    create_claude_session_factory,
)
from lup.runtime.wrappers import (
    BudgetConfig,
    CorrectionConfig,
    PersistenceConfig,
    RecoveryConfig,
    TimeoutConfig,
    decorated_session_factory,
)
from lup.types import Usage

from examples.common import Summary


def reported_cost(usage: Usage) -> float:
    """Use provider-reported cost while treating an absent estimate as zero."""
    return usage.cost_usd or 0.0


async def main() -> None:
    native = create_claude_session_factory(
        ClaudeSessionConfig(
            model="claude-opus-5",
            system_prompt="Submit a concise structured result.",
        )
    )
    factory = decorated_session_factory(
        native,
        timeout=TimeoutConfig(seconds=120),
        budget=BudgetConfig(maximum_usd=1.0, usage_cost=reported_cost),
        recovery=RecoveryConfig(retries=1),
        correction=CorrectionConfig(cycles=1),
        persistence=PersistenceConfig(directory=Path("tmp/example-results")),
        serialized=True,
    )
    result = await factory.query("Explain this wrapper stack.", Summary)
    print(result.output.summary)


if __name__ == "__main__":
    asyncio.run(main())
