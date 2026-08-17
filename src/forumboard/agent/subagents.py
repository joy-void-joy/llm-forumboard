# lup: ignore[constant-declaration]
# The prompts here are each subagent's own standing prose and the roster is
# which subagents this application declares — both are what this module is for,
# and a project wanting others writes them here.
"""Subagent definitions.

This is a TEMPLATE. Define subagents for specialized tasks in your domain.

Subagents are spawned by the main agent to perform focused work.
Each subagent has:
- A specialized prompt (focused on one job)
- A subset of tools (only what it needs)
- Its own model (cheaper models for simpler tasks)

Definitions use ``SubagentSpec`` as portable data. The application injects a
typed factory recipe into :func:`lup.subagents.create_run_subagent_tool`; the
tool then performs a one-shot query without selecting a provider itself.

A spec without a ``model`` inherits the session's main model on every
backend; pinning one (as the specs below do) is a deliberate cost/skill
choice that holds regardless of ``AGENT_SDK``.

Subagents are one of several agent shapes — ``docs/orchestration.md`` is
the full catalog. Where the siblings live:

- Nested agents: a one-shot :func:`lup.runtime.query.query` inside a
  tool handler; the reviewer in ``agent/tools/reflect.py`` is the
  exemplar
- Background agents: :class:`lup.runtime.background.BackgroundAgent`, with
  the observer example in ``agent/tools/realtime.py``
- Persistent agents: ``lup.realtime.scheduler`` and ``lup.realtime.relay``,
  with example tools in ``agent/tools/realtime.py``
- Data augmentation: ``agent/tools/example.py`` (domain dispatch,
  null-filling, extraction)
"""

from lup.types import SubagentSpec

# =============================================================================
# lup: template: tool lists — grant each subagent only the tools its job needs
# =============================================================================


def research_tools() -> list[str]:
    """Names of the tools a research subagent is allowed to call.

    A function rather than a constant so that a tool which depends on a
    configured API key can be added conditionally, keeping that choice
    beside the rest of the selection. Resolved at import here; to vary it
    per session, call it from :func:`get_subagent_specs` instead.
    """
    return [
        "WebSearch",
        "WebFetch",
        "Read",
        "Glob",
    ]


def analysis_tools() -> list[str]:
    """Names of the tools an analysis subagent is allowed to call."""
    return [
        "Read",
        "Glob",
    ]


# =============================================================================
# lup: template: subagent definitions — replace researcher/analyzer with your
# domain's specialists (each spec: prompt, tool subset, pinned model)
# =============================================================================


RESEARCHER_PROMPT = """\
You are a research assistant gathering information on a topic.

## Your Task
Research the topic/question given to you. Your output should be thorough and factual.

## Approach
1. Search for relevant information
2. Verify facts across multiple sources
3. Note any uncertainties or contradictions
4. Organize findings clearly

## Output Format (JSON)
```json
{
  "key_facts": ["Fact 1 with source", "Fact 2 with source"],
  "uncertainties": ["What we don't know"],
  "sources": [{"title": "...", "url": "..."}],
  "summary": "Brief synthesis of findings"
}
```
"""

researcher = SubagentSpec(
    name="researcher",
    description=(
        "Research agent for gathering information. Searches multiple sources, "
        "verifies facts, and returns organized findings."
    ),
    prompt=RESEARCHER_PROMPT,
    tools=research_tools(),
    model="claude-opus-5",
)


ANALYZER_PROMPT = """\
You are an analysis assistant examining data or content.

## Your Task
Analyze the given data/content and extract insights.

## Approach
1. Understand what you're analyzing
2. Identify patterns and anomalies
3. Draw conclusions
4. Note confidence levels

## Output Format (JSON)
```json
{
  "insights": ["Insight 1", "Insight 2"],
  "patterns": ["Pattern observed"],
  "anomalies": ["Unusual finding"],
  "confidence": 0.8,
  "summary": "Brief analysis summary"
}
```
"""

analyzer = SubagentSpec(
    name="analyzer",
    description=(
        "Analysis agent for examining data and extracting insights. "
        "Identifies patterns, anomalies, and draws conclusions."
    ),
    prompt=ANALYZER_PROMPT,
    tools=analysis_tools(),
    model="claude-opus-5",
)


# =============================================================================
# EXPORTED SUBAGENTS
# =============================================================================

ALL_SPECS: list[SubagentSpec] = [researcher, analyzer]


def get_subagent_specs() -> list[SubagentSpec]:
    """Return all subagent specs (SDK-agnostic).

    Each adapter converts these into its native primitive at build time.
    """
    return list(ALL_SPECS)
