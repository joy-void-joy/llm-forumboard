"""Point the Claude adapter at an Anthropic-compatible local endpoint."""

import asyncio

from lup.adapters.claude.config import (
    ClaudeCompatibilityTransform,
    ClaudeCompatibleEndpoint,
)
from lup.adapters.claude.runtime import (
    ClaudeSessionConfig,
    create_claude_session_factory,
)

from examples.common import Summary


async def main() -> None:
    base = ClaudeSessionConfig(
        model="local-model",
        system_prompt="Submit a concise structured summary.",
    )
    endpoint = ClaudeCompatibleEndpoint.model_validate(
        {"base_url": "http://localhost:4000"}
    )
    configured = ClaudeCompatibilityTransform(endpoint).apply(base)
    result = await create_claude_session_factory(configured).query(
        "Confirm the compatible endpoint.", Summary
    )
    print(result.output.summary)


if __name__ == "__main__":
    asyncio.run(main())
