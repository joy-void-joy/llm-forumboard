"""Run one typed Claude turn as an operation on the configured factory."""

import asyncio

from lup.adapters.claude.runtime import (
    ClaudeSessionConfig,
    create_claude_session_factory,
)

from examples.common import Summary


async def main() -> None:
    factory = create_claude_session_factory(
        ClaudeSessionConfig(
            model="claude-opus-5",
            system_prompt="Return a concise summary through submit_output.",
        )
    )
    result = await factory.query("Summarize why typed boundaries help.", Summary)
    print(result.output.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())
