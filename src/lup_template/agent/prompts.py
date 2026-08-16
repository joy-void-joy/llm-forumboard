# lup: ignore[constant-declaration]
# Every constant here is a block of this agent's own standing prose. What it is
# told is the thing this module decides — a project wanting different words
# writes them here, which is what a scaffold is for.
"""The standing prose the agent is told: its task, its guidelines, and how to
deliver a result.

This is a TEMPLATE. Customize for your domain.

This is the agent's brief, not its toolbox. How to use any one tool lives in
that tool's own description, which the agent reads directly — repeating tool
names here would only create a second list to keep in sync (see Tool Design
Philosophy in CLAUDE.md). What stays here is everything no single tool can
say: the job to do, how to approach it, and what a finished result looks like.

The brief is assembled from named sections (:data:`SECTIONS`) so you can add,
drop, or reorder one piece without touching the rest. Two pieces stay correct
on their own: today's date is filled in from a ``{date}`` placeholder, and the
output-format section is generated from :class:`~lup_template.agent.models.AgentOutput`,
so editing that model is what changes the result the agent is asked to submit.
"""

from datetime import datetime

from lup_template.agent.models import AgentOutput

# ---------------------------------------------------------------------------
# lup: template: prompt sections — fill in PURPOSE and GUIDELINES for your
# domain; output_format() already follows AgentOutput automatically
# ---------------------------------------------------------------------------

INTRO = """\
You are an AI agent. Today's date is {date}."""

PURPOSE = """\
## Your Task

[Describe what the agent does]"""


def output_format() -> str:
    """Render the output-format section from the ``AgentOutput`` schema.

    Fields and their descriptions come straight from the model, so adding
    or renaming a field in models.py updates the prompt automatically —
    avoiding the two-sources-of-truth drift CLAUDE.md warns against.
    """
    lines = [
        "## Output Format",
        "",
        "When your analysis is complete, submit your final result with the",
        "submit_output tool — the session's result is exactly what you submit",
        "there. Reflect with the review tool first; submission is rejected",
        "until you have. Your submission includes:",
    ]
    for name, schema in AgentOutput.model_json_schema()["properties"].items():
        match schema:
            case {"description": str(description)} if description:
                lines.append(f"- **{name}**: {description}")
            case _:
                lines.append(f"- **{name}**")
    return "\n".join(lines)


GUIDELINES = """\
## Guidelines

1. Think step by step
2. Use your available tools to gather information before reasoning
3. Be explicit about uncertainty
4. Document your reasoning

[Add domain-specific guidelines here]"""


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------

SECTIONS: list[str] = [
    INTRO,
    PURPOSE,
    output_format(),
    GUIDELINES,
]


def get_system_prompt(
    *,
    date: datetime | None = None,
    extra_sections: list[str] | None = None,
) -> str:
    """Generate the system prompt by composing sections.

    Only the section declaring the ``{date}`` placeholder is substituted,
    so customized sections may contain literal braces (e.g. a JSON example)
    without raising.

    Args:
        date: Date to use as "today". If None, uses current date.
        extra_sections: Additional prompt sections appended after SECTIONS.

    Returns:
        The formatted system prompt.
    """
    effective_date = (date or datetime.now()).strftime("%Y-%m-%d")
    rendered = [
        section.format(date=effective_date) if "{date}" in section else section
        for section in SECTIONS
    ]
    if extra_sections:
        rendered.extend(extra_sections)

    return "\n\n".join(rendered) + "\n"
