"""Refuse a denied command inside a live session — the tool-call path.

The tool-call half of the policy pair; ``semantic_policy`` is the fetch
half. The shell policy consults the same declared origin table, so the URL
refused to a fetch is refused to ``curl`` as well and no command reaches a
shell by taking the other route.

The session runs with permissions bypassed, which is the mode where this
hook is the only gate the call meets: what the policy denies is refused,
and nothing else is left to grant it.
"""

import asyncio

from pydantic import AnyHttpUrl

from lup.adapters.claude.hooks import CLAUDE_SEMANTICS
from lup.adapters.claude.runtime import (
    ClaudeSandboxConfig,
    ClaudeSessionConfig,
    create_claude_session_factory,
)
from lup.hooks import LupHooksConfig
from lup.policy.enforcement import SemanticToolPolicy, create_policy_hooks
from lup.policy.rules import ShellPolicy, UrlScope
from lup.runtime.query import query
from lup_template.devtools.harness.content.shell_vocabulary import SHELL_RULES

from examples.common import Summary

# lup: ignore[constant-declaration] — the one origin this example allows, which
# is the example's subject rather than a value to pass in
DOCS_ORIGIN = AnyHttpUrl("https://docs.example.com")
# lup: ignore[constant-declaration] — the one command this example demonstrates a
# denial on, which is the example's subject rather than a value to pass in
DENIED_COMMAND = "curl https://docs.example.com/private/token"

SANDBOX = ClaudeSandboxConfig(allow_unsandboxed_commands=True)
"""The escape this session permits, said once to the policy and the runtime.

A rule may place a call outside the sandbox, but only a session that opened
the channel can carry it there. One object answers both so the two cannot
disagree — unstated, a placement is rendered and silently dropped.
"""


def policy_hooks() -> LupHooksConfig:
    """Enforce the shell lattice, scoped by the same declared origins.

    The vocabulary is this project's own: read-only commands allow,
    destructive ones ask, and anything the lattice cannot judge denies with
    the recipe for reshaping or escalating it.
    """
    policy = ShellPolicy(
        SHELL_RULES,
        allowed_urls=[UrlScope(origin=DOCS_ORIGIN, path_prefix="/api")],
        denied_urls=[UrlScope(origin=DOCS_ORIGIN, path_prefix="/private")],
    )
    return create_policy_hooks(
        SemanticToolPolicy(shell=policy),
        CLAUDE_SEMANTICS,
        sandbox=SANDBOX.posture(),
    )


def session_config() -> ClaudeSessionConfig:
    """Carry the enforcing hooks into the session the factory will open."""
    return ClaudeSessionConfig(
        model="claude-opus-5",
        system_prompt="Run what you are asked to run and report what happened.",
        hooks=policy_hooks(),
        sandbox=SANDBOX,
    )


async def main() -> None:
    factory = create_claude_session_factory(session_config())
    result = await query(
        factory,
        f"Run `{DENIED_COMMAND}` and summarize the output.",
        Summary,
    )
    print(result.output.summary)


if __name__ == "__main__":
    asyncio.run(main())
