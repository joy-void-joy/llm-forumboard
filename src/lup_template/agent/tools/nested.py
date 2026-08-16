"""Nested Agent pattern (template).

A nested agent is an MCP tool that, inside its handler, spins up an
independent configured session via :func:`lup.runtime.query.query`, runs it to
completion, and folds the scalar result back into a structured tool response.

It differs from a **native subagent** (defined upfront in ``get_subagent_specs``
and sharing the main session and trace): a nested agent — a *tool-subagent* —
is created on demand and keeps its context fully separate, so the main agent receives only the
conclusion, not the reasoning chain. The tool handler is the context boundary —
it post-processes ("augments") the nested agent's raw output into the response.

This is a TEMPLATE. Replace the example with the quick, context-separable work
your domain needs (generation, parsing, scoring, a second opinion), and add
``NESTED_TOOLS`` to a server group in ``toolsets.py`` to serve it. See
docs/orchestration.md § Nested Agent Pattern. The application composition root builds
the auxiliary factory with ``aux_model()``, like the reviewer in ``reflect.py``.
"""

from pydantic import BaseModel, Field

from lup.mcp import lup_tool
from lup.runtime.query import query
from lup_template.agent.config import aux_model


class CritiqueInput(BaseModel):
    """Input for the nested critique tool."""

    draft: str = Field(description="The text to get an independent second opinion on")
    focus: str = Field(
        default="clarity and correctness",
        description="What the nested reviewer should weigh most heavily",
    )


class CritiqueOutput(BaseModel):
    """The nested agent's critique, folded into structured output by the tool."""

    critique: str = Field(
        description="The nested agent's assessment, bounded by the tool"
    )
    truncated: bool = Field(
        description="Whether the tool shortened the output — the tool owns this, "
        "not the nested agent"
    )


# The handler is the context boundary, and that is the operation here: opening
# a session, bounding its output, folding it into CritiqueOutput. CritiqueInput
# is the schema `lup_tool` infers the tool's arguments from, so it names how the
# agent calls in rather than a subject the critique could be declared on.
# lup: ignore[model-free-function] — MCP tool handler; CritiqueInput is its input schema
@lup_tool(
    "Get an independent second opinion on a draft from a nested agent. Use when "
    "the main agent wants a fresh critique without spending its own context on a "
    "full review pass: the nested agent reads the draft in isolation and returns "
    "only its assessment. Exists to keep review reasoning out of the main "
    "agent's context window. Returns {critique, truncated}."
)
async def critique(params: CritiqueInput) -> CritiqueOutput:
    """Run a nested reviewer and augment its raw text into the response."""

    from lup_template.agent.core import build_auxiliary_factory

    factory = build_auxiliary_factory(model=aux_model())
    result = await query(
        factory,
        (
            f"Critique the following draft, focusing on {params.focus}. "
            f"Be specific and concise.\n\n{params.draft}"
        ),
    )
    text = "\n\n".join(
        text for block in result.blocks if (text := block.text_payload) is not None
    )
    limit = 2000
    # Augment: the tool, not the nested agent, bounds and shapes the output.
    return CritiqueOutput(critique=text[:limit], truncated=len(text) > limit)


# lup: ignore[constant-declaration] — which tools this module defines, which is
# the module's own roster rather than a value a caller supplies
NESTED_TOOLS = [critique]
"""Nested-agent tools — add to a server group in ``toolsets.py`` to serve them."""
