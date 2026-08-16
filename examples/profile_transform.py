"""Resolve a Claude profile into its configured session factory."""

import asyncio
from pathlib import Path

from lup.adapters.claude.config import (
    ClaudeProfileRegistry,
    ClaudeProfileSelection,
    claude_profile_selector,
)
from lup.adapters.claude.runtime import ClaudeSessionConfig

from examples.common import Summary


async def main() -> None:
    base = ClaudeSessionConfig(
        model="claude-opus-5",
        system_prompt="Submit a concise structured summary.",
    )
    registry = ClaudeProfileRegistry(
        profiles={
            "work": ClaudeProfileSelection(
                config_directory=Path.home() / ".claude-work"
            )
        },
        active="work",
    )
    factory = claude_profile_selector(registry).session_factory(base)
    result = await factory.query("Describe immutable configuration.", Summary)
    print(result.output.summary)


if __name__ == "__main__":
    asyncio.run(main())
